from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from memory_ops.config import Settings
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.retrieval import (
    EmbeddingModel,
    EmbeddingPolicy,
    HashEmbeddingProvider,
    RetrievalService,
)
from memory_ops.user_memory import CorrectionRequest, MemoryScope, RememberRequest, UserMemoryService
from memory_ops.workers import EmbeddingWorker, OutboxEvent, OutboxWorker


@pytest.fixture(scope="module")
def vector_engine() -> Engine:
    engine = create_database_engine(Settings.from_environment().database_url)
    upgrade_database(engine)
    yield engine
    engine.dispose()


def context(engine: Engine) -> tuple[TenantDatabase, MemoryScope, UserMemoryService]:
    tenant_id, workspace_id, subject_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants (id) VALUES (:id)"), {"id": tenant_id})
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant_id)"),
            {"id": workspace_id, "tenant_id": tenant_id},
        )
    database = TenantDatabase(engine)
    scope = MemoryScope(tenant_id=tenant_id, workspace_id=workspace_id, subject_id=subject_id)
    return database, scope, UserMemoryService(database, "policy-2026-10")


def remember(
    writer: UserMemoryService,
    scope: MemoryScope,
    statement: str,
    *,
    sensitivity: str = "normal",
) -> tuple[UUID, UUID]:
    result = writer.remember(
        RememberRequest(
            scope=scope,
            semantic_type="fact",
            statement=statement,
            sensitivity=sensitivity,
            purpose="planning",
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        f"remember-{uuid4()}",
    )
    return result.receipt.resource_id, result.receipt.resource_version


def claimed(database: TenantDatabase, scope: MemoryScope) -> list[OutboxEvent]:
    return OutboxWorker(database).claim(scope.tenant_id)


def worker(database: TenantDatabase, provider=HashEmbeddingProvider()) -> EmbeddingWorker:
    return EmbeddingWorker(
        database,
        provider,
        EmbeddingPolicy({"local": frozenset({"normal", "sensitive"})}),
        "generation-1",
    )


def test_embedding_worker_records_canonical_model_identity_and_vector_candidates(
    vector_engine: Engine,
) -> None:
    database, scope, writer = context(vector_engine)
    atlas_id, _ = remember(writer, scope, "Atlas project deadline is 18 October 2026.")
    remember(writer, scope, "The user prefers a window seat on flights.")
    embedder = HashEmbeddingProvider()
    indexer = worker(database, embedder)
    for event in claimed(database, scope):
        assert indexer.process(scope.tenant_id, event) is not None

    results = RetrievalService(database).vector(
        scope,
        embedder.embed("Atlas project deadline is 18 October 2026."),
        purpose="planning",
        model=embedder.metadata,
        index_generation="generation-1",
    )

    assert results[0].memory_id == atlas_id
    assert results[0].channel == "vector"
    with database.transaction(scope.tenant_id) as connection:
        metadata = connection.execute(
            text(
                """
                SELECT canonical_version_id, index_generation, provider,
                       model_name, model_version
                FROM user_memory_embeddings
                WHERE memory_id = :memory_id
                """
            ),
            {"memory_id": atlas_id},
        ).one()
    assert tuple(metadata[1:]) == ("generation-1", "local", "hash-ngrams", "1.0.0")


@dataclass
class RecordingProvider:
    metadata: EmbeddingModel = EmbeddingModel("local", "recording", "1.0.0")
    calls: list[str] = field(default_factory=list)

    def embed(self, content: str) -> tuple[float, ...]:
        self.calls.append(content)
        return (0.0,) * self.metadata.dimensions


def test_policy_denial_prevents_provider_disclosure_and_persistence(
    vector_engine: Engine,
) -> None:
    database, scope, writer = context(vector_engine)
    memory_id, _ = remember(
        writer,
        scope,
        "Restricted planning detail.",
        sensitivity="restricted",
    )
    provider = RecordingProvider()
    event = claimed(database, scope)[0]

    assert worker(database, provider).process(scope.tenant_id, event) is None
    assert provider.calls == []
    assert OutboxWorker(database).status(scope.tenant_id, event.id).status == "completed"
    with database.transaction(scope.tenant_id) as connection:
        count = connection.execute(
            text("SELECT count(*) FROM user_memory_embeddings WHERE memory_id = :id"),
            {"id": memory_id},
        ).scalar_one()
    assert count == 0


def test_stale_version_event_cannot_publish_an_embedding(vector_engine: Engine) -> None:
    database, scope, writer = context(vector_engine)
    memory_id, old_version_id = remember(writer, scope, "Atlas deadline was Friday.")
    corrected = writer.correct(
        CorrectionRequest(
            scope=scope,
            memory_id=memory_id,
            statement="Atlas retrospective is complete.",
        ),
        f"correct-{uuid4()}",
    )
    events = claimed(database, scope)
    indexer = worker(database)

    old_event = next(event for event in events if event.resource_version == old_version_id)
    current_event = next(
        event for event in events if event.resource_version == corrected.receipt.resource_version
    )
    assert indexer.process(scope.tenant_id, old_event) is None
    assert indexer.process(scope.tenant_id, current_event).canonical_version_id == corrected.receipt.resource_version
    with database.transaction(scope.tenant_id) as connection:
        versions = connection.execute(
            text("SELECT canonical_version_id FROM user_memory_embeddings WHERE memory_id = :id"),
            {"id": memory_id},
        ).scalars().all()
    assert versions == [corrected.receipt.resource_version]

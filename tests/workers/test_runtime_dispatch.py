from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text

from memory_ops.config import Settings
from memory_ops.lifecycle import LifecycleService, PurgeService
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.retrieval import EmbeddingPolicy, HashEmbeddingProvider
from memory_ops.user_memory import MemoryScope, RememberRequest, UserMemoryService
from memory_ops.worker import process_once
from memory_ops.workers import EmbeddingWorker, OutboxWorker, PurgeWorker


@pytest.fixture(scope="module")
def engine() -> Engine:
    value = create_database_engine(Settings.from_environment().database_url)
    upgrade_database(value)
    yield value
    value.dispose()


def runtime(engine: Engine, *, lease_seconds: int = 60):
    database = TenantDatabase(engine)
    outbox = OutboxWorker(database, lease_seconds=lease_seconds)
    embeddings = EmbeddingWorker(
        database,
        HashEmbeddingProvider(),
        EmbeddingPolicy({"local": frozenset({"normal", "sensitive"})}),
        "generation-1",
    )
    return database, outbox, embeddings, PurgeWorker(database)


def memory(database: TenantDatabase, tenant_id):
    workspace_id, subject_id = uuid4(), uuid4()
    with database.engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants (id) VALUES (:id)"), {"id": tenant_id})
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant_id)"),
            {"id": workspace_id, "tenant_id": tenant_id},
        )
    scope = MemoryScope(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        subject_id=subject_id,
    )
    result = UserMemoryService(database, "worker-test-policy").remember(
        RememberRequest(
            scope=scope,
            semantic_type="preference",
            statement="The user prefers a window seat.",
            purpose="travel",
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        f"remember-{uuid4()}",
    )
    return scope, result.receipt.resource_id, result.receipt.resource_version


def test_dispatches_embedding_and_purge_events(engine: Engine) -> None:
    tenant_id = uuid4()
    database, outbox, embeddings, purges = runtime(engine)
    scope, memory_id, version_id = memory(database, tenant_id)

    assert process_once(tenant_id, outbox, embeddings, purges) == 1
    with database.transaction(tenant_id) as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM user_memory_embeddings "
                "WHERE canonical_version_id = :version_id"
            ),
            {"version_id": version_id},
        ).scalar_one() == 1

    LifecycleService(database).forget(scope, memory_id, f"forget-{uuid4()}")
    assert process_once(tenant_id, outbox, embeddings, purges) == 1
    assert PurgeService(database).status(tenant_id, memory_id).deletion_completeness == 1.0


def test_failure_retries_and_expired_lease_is_reclaimed(engine: Engine) -> None:
    tenant_id = uuid4()
    database, outbox, embeddings, purges = runtime(engine, lease_seconds=0)
    _, _, version_id = memory(database, tenant_id)
    claimed = outbox.claim(tenant_id)
    assert len(claimed) == 1

    def fail(*_args, **_kwargs):
        raise RuntimeError("simulated failure")

    embeddings.process = fail
    assert process_once(
        tenant_id,
        outbox,
        embeddings,
        purges,
        retry_delay_seconds=0,
    ) == 1

    with database.transaction(tenant_id) as connection:
        event = connection.execute(
            text(
                "SELECT status, attempts, last_error_code FROM outbox_events "
                "WHERE resource_version = :version_id"
            ),
            {"version_id": version_id},
        ).one()
    assert tuple(event) == ("pending", 2, "RuntimeError")

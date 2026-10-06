from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, inspect, text

from memory_ops.config import Settings
from memory_ops.lifecycle import (
    PURGE_TARGETS,
    LifecycleService,
    PurgeIncomplete,
    PurgeService,
)
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.user_memory import EvidenceReference, MemoryScope, RememberRequest, UserMemoryService
from memory_ops.workers import OutboxWorker, PurgeWorker


@pytest.fixture(scope="module")
def purge_store() -> tuple[Engine, TenantDatabase, MemoryScope]:
    engine = create_database_engine(Settings.from_environment().database_url)
    upgrade_database(engine)
    tenant_id, workspace_id, subject_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants (id) VALUES (:id)"), {"id": tenant_id})
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant_id)"),
            {"id": workspace_id, "tenant_id": tenant_id},
        )
    database = TenantDatabase(engine)
    yield engine, database, MemoryScope(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        subject_id=subject_id,
    )
    engine.dispose()


def forgotten_memory(database: TenantDatabase, scope: MemoryScope) -> tuple[UUID, UUID]:
    memory = UserMemoryService(database, "policy-2026-10").remember(
        RememberRequest(
            scope=scope,
            semantic_type="fact",
            statement="The user lives in Pune.",
            purpose="assistant_context",
            evidence=(EvidenceReference(evidence_type="event", reference_id="event-1"),),
        ),
        f"remember-{uuid4()}",
    )
    with database.transaction(scope.tenant_id) as connection:
        connection.execute(
            text(
                """
                INSERT INTO user_memory_embeddings (
                    tenant_id, memory_id, canonical_version_id,
                    index_generation, provider, model_name, model_version,
                    embedding
                ) VALUES (
                    :tenant_id, :memory_id, :version_id,
                    'test-generation', 'local', 'test', '1.0.0',
                    CAST(:embedding AS vector)
                )
                """
            ),
            {
                "tenant_id": scope.tenant_id,
                "memory_id": memory.receipt.resource_id,
                "version_id": memory.receipt.resource_version,
                "embedding": "[" + ",".join(["0"] * 64) + "]",
            },
        )
    LifecycleService(database).forget(scope, memory.receipt.resource_id, f"forget-{uuid4()}")
    return memory.receipt.resource_id, memory.receipt.resource_version


def test_purge_is_complete_ordered_idempotent_and_non_reconstructive(
    purge_store: tuple[Engine, TenantDatabase, MemoryScope],
) -> None:
    engine, database, scope = purge_store
    memory_id, version_id = forgotten_memory(database, scope)
    purge = PurgeService(database)

    columns = {
        column["name"]
        for column in inspect(engine).get_columns("memory_deletion_tombstones")
    }
    assert columns == {
        "tenant_id",
        "memory_id",
        "deleted_version_id",
        "deletion_generation",
        "reason",
        "requested_at",
        "completed_at",
    }

    with pytest.raises(PurgeIncomplete, match="dependent purge targets"):
        purge.purge_target(scope.tenant_id, memory_id, "canonical")
    assert purge.status(scope.tenant_id, memory_id).deletion_completeness == 0.0

    outbox = OutboxWorker(database)
    event = next(
        item
        for item in outbox.claim(scope.tenant_id)
        if item.event_type == "user_memory.purge.requested" and item.resource_id == memory_id
    )
    status = PurgeWorker(database).process(scope.tenant_id, event)

    assert status.deletion_completeness == 1.0
    assert set(status.completed_targets) == set(PURGE_TARGETS)
    assert outbox.status(scope.tenant_id, event.id).status == "completed"
    assert purge.purge_all(scope.tenant_id, memory_id) == status
    with database.transaction(scope.tenant_id) as connection:
        row = connection.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM user_memories WHERE id = :memory_id),
                    (SELECT count(*) FROM user_memory_versions WHERE memory_id = :memory_id),
                    (SELECT count(*) FROM user_memory_evidence WHERE memory_version_id = :version_id),
                    (SELECT count(*) FROM user_memory_embeddings WHERE memory_id = :memory_id),
                    (SELECT count(*) FROM memory_purge_receipts WHERE memory_id = :memory_id),
                    (SELECT count(*) FROM memory_deletion_tombstones WHERE memory_id = :memory_id AND completed_at IS NOT NULL),
                    (SELECT array_agg(attempts ORDER BY target) FROM memory_purge_receipts WHERE memory_id = :memory_id)
                """
            ),
            {"memory_id": memory_id, "version_id": version_id},
        ).one()
    assert tuple(row[:6]) == (0, 0, 0, 0, len(PURGE_TARGETS), 1)
    assert row[6] == [1] * len(PURGE_TARGETS)

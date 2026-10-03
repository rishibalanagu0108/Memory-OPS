from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from memory_ops.config import Settings
from memory_ops.persistence import (
    TenantDatabase,
    create_database_engine,
    upgrade_database,
)
from memory_ops.persistence.writes import IdempotencyConflict
from memory_ops.security import ProhibitedContent
from memory_ops.user_memory import MemoryScope, RememberRequest, UserMemoryService


@pytest.fixture(scope="module")
def store() -> tuple[Engine, UserMemoryService, UUID, UUID, UUID]:
    engine = create_database_engine(Settings().database_url)
    upgrade_database(engine)
    tenant_id, workspace_id, subject_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:tenant_id)"),
            {"tenant_id": tenant_id},
        )
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant_id)"),
            {"id": workspace_id, "tenant_id": tenant_id},
        )
    service = UserMemoryService(TenantDatabase(engine), "policy-2026-10")
    yield engine, service, tenant_id, workspace_id, subject_id
    engine.dispose()


def request_for(
    tenant_id: UUID,
    workspace_id: UUID,
    subject_id: UUID,
    semantic_type: str = "preference",
    statement: str = "The user prefers concise answers.",
) -> RememberRequest:
    return RememberRequest.model_validate(
        {
            "scope": {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "subject_id": subject_id,
            },
            "semantic_type": semantic_type,
            "statement": statement,
            "sensitivity": "normal",
            "lifetime": "durable",
            "purpose": "assistant_context",
        }
    )


@pytest.mark.parametrize(
    ("semantic_type", "statement"),
    [
        ("fact", "The user lives in Pune."),
        ("preference", "The user prefers concise answers."),
        ("goal", "The user wants to ship Memory-ops."),
        ("constraint", "Do not send user content to external models."),
        ("episode", "The user approved the M1 architecture."),
    ],
)
def test_explicit_admission_preserves_all_semantics_and_classifies_policy(
    store: tuple[Engine, UserMemoryService, UUID, UUID, UUID],
    semantic_type: str,
    statement: str,
) -> None:
    engine, service, tenant_id, workspace_id, subject_id = store
    result = service.remember(
        request_for(tenant_id, workspace_id, subject_id, semantic_type, statement),
        f"semantic-{semantic_type}-{uuid4()}",
    )

    with TenantDatabase(engine).transaction(tenant_id) as connection:
        row = connection.execute(
            text(
                """
                SELECT m.semantic_type, m.current_version_id, v.original_statement,
                       v.normalized_value, v.sensitivity, v.lifetime, v.origin,
                       v.purpose, v.policy_version
                FROM user_memories m
                JOIN user_memory_versions v ON v.id = m.current_version_id
                WHERE m.id = :memory_id
                """
            ),
            {"memory_id": result.receipt.resource_id},
        ).one()
        event_count = connection.execute(
            text("SELECT count(*) FROM outbox_events WHERE resource_id = :memory_id"),
            {"memory_id": result.receipt.resource_id},
        ).scalar_one()

    assert row.semantic_type == semantic_type
    assert row.current_version_id == result.receipt.resource_version
    assert row.original_statement == statement
    assert row.normalized_value == statement
    assert (row.sensitivity, row.lifetime, row.origin) == (
        "normal",
        "durable",
        "explicit",
    )
    assert (row.purpose, row.policy_version) == (
        "assistant_context",
        "policy-2026-10",
    )
    assert event_count == 1


def test_identical_retries_return_one_canonical_mutation(
    store: tuple[Engine, UserMemoryService, UUID, UUID, UUID],
) -> None:
    engine, service, tenant_id, workspace_id, subject_id = store
    request = request_for(tenant_id, workspace_id, subject_id)
    key = f"retry-{uuid4()}"

    first = service.remember(request, key)
    replay = service.remember(request, key)

    with engine.connect() as connection:
        counts = connection.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM user_memories WHERE id = :memory_id),
                    (SELECT count(*) FROM user_memory_versions WHERE memory_id = :memory_id),
                    (SELECT count(*) FROM outbox_events WHERE resource_id = :memory_id)
                """
            ),
            {"memory_id": first.receipt.resource_id},
        ).one()

    assert replay.replayed is True
    assert replay.receipt == first.receipt
    assert tuple(counts) == (1, 1, 1)


def test_conflicting_retry_is_rejected_without_another_mutation(
    store: tuple[Engine, UserMemoryService, UUID, UUID, UUID],
) -> None:
    engine, service, tenant_id, workspace_id, subject_id = store
    key = f"conflict-{uuid4()}"
    first = service.remember(
        request_for(tenant_id, workspace_id, subject_id), key
    )

    with pytest.raises(IdempotencyConflict):
        service.remember(
            request_for(
                tenant_id,
                workspace_id,
                subject_id,
                statement="The user prefers detailed answers.",
            ),
            key,
        )

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM user_memory_versions WHERE memory_id = :id"),
            {"id": first.receipt.resource_id},
        ).scalar_one() == 1


def test_prohibited_secret_is_rejected_before_any_write(
    store: tuple[Engine, UserMemoryService, UUID, UUID, UUID],
) -> None:
    engine, service, tenant_id, workspace_id, subject_id = store
    key = f"secret-{uuid4()}"

    with pytest.raises(ProhibitedContent):
        service.remember(
            request_for(
                tenant_id,
                workspace_id,
                subject_id,
                semantic_type="fact",
                statement="api_key=sk-test-only-prohibited-sentinel",
            ),
            key,
        )

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM idempotency_records WHERE idempotency_key = :key"),
            {"key": key},
        ).scalar_one() == 0


def test_outbox_failure_rolls_back_the_entire_canonical_write(
    store: tuple[Engine, UserMemoryService, UUID, UUID, UUID],
) -> None:
    engine, service, tenant_id, workspace_id, subject_id = store
    key = f"atomic-{uuid4()}"
    with TenantDatabase(engine).transaction(tenant_id) as connection:
        before = connection.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM user_memories),
                    (SELECT count(*) FROM user_memory_versions),
                    (SELECT count(*) FROM outbox_events)
                """
            )
        ).one()
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                CREATE OR REPLACE FUNCTION reject_test_outbox_write()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.tenant_id = '{tenant_id}'::uuid THEN
                        RAISE EXCEPTION 'simulated outbox failure';
                    END IF;
                    RETURN NEW;
                END;
                $$
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TRIGGER reject_test_outbox_write
                BEFORE INSERT ON outbox_events
                FOR EACH ROW EXECUTE FUNCTION reject_test_outbox_write()
                """
            )
        )

    try:
        with pytest.raises(DBAPIError, match="simulated outbox failure"):
            service.remember(
                request_for(tenant_id, workspace_id, subject_id), key
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TRIGGER reject_test_outbox_write ON outbox_events"))
            connection.execute(text("DROP FUNCTION reject_test_outbox_write"))

    with TenantDatabase(engine).transaction(tenant_id) as connection:
        after = connection.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM user_memories),
                    (SELECT count(*) FROM user_memory_versions),
                    (SELECT count(*) FROM outbox_events)
                """
            )
        ).one()
        idempotency_count = connection.execute(
            text("SELECT count(*) FROM idempotency_records WHERE idempotency_key = :key"),
            {"key": key},
        ).scalar_one()
    assert after == before
    assert idempotency_count == 0

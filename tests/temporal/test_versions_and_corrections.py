from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from memory_ops.config import Settings
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.user_memory import (
    CorrectionRequest,
    MemoryScope,
    RememberRequest,
    UserMemoryService,
)


@pytest.fixture(scope="module")
def temporal_store() -> tuple[Engine, UserMemoryService, MemoryScope]:
    engine = create_database_engine(Settings.from_environment().database_url)
    upgrade_database(engine)
    tenant_id, workspace_id, subject_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
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
    yield engine, UserMemoryService(TenantDatabase(engine), "policy-2026-10"), scope
    engine.dispose()


def remember(service: UserMemoryService, scope: MemoryScope, statement: str) -> UUID:
    result = service.remember(
        RememberRequest(
            scope=scope,
            semantic_type="preference",
            statement=statement,
            purpose="assistant_context",
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        f"remember-{uuid4()}",
    )
    return result.receipt.resource_id


def test_correction_appends_an_immutable_linked_version_and_moves_current_pointer(
    temporal_store: tuple[Engine, UserMemoryService, MemoryScope],
) -> None:
    engine, service, scope = temporal_store
    memory_id = remember(service, scope, "The user prefers tea.")
    original = service.current_version(scope, memory_id)

    result = service.correct(
        CorrectionRequest(
            scope=scope,
            memory_id=memory_id,
            statement="The user prefers coffee.",
        ),
        f"correct-{uuid4()}",
    )
    corrected = service.current_version(scope, memory_id)

    assert corrected.id == result.receipt.resource_version
    assert corrected.original_statement == "The user prefers coffee."
    assert corrected.version_number == 2
    assert corrected.supersedes_version_id == original.id
    assert corrected.change_kind == "correction"
    with TenantDatabase(engine).transaction(scope.tenant_id) as connection:
        versions = connection.execute(
            text(
                "SELECT id, original_statement FROM user_memory_versions "
                "WHERE memory_id = :memory_id ORDER BY version_number"
            ),
            {"memory_id": memory_id},
        ).all()
    assert versions == [
        (original.id, "The user prefers tea."),
        (corrected.id, "The user prefers coffee."),
    ]

    with pytest.raises(DBAPIError, match="versions are immutable"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE user_memory_versions SET original_statement = 'changed' WHERE id = :id"),
                {"id": original.id},
            )


def test_valid_and_recorded_time_queries_return_the_version_known_then(
    temporal_store: tuple[Engine, UserMemoryService, MemoryScope],
) -> None:
    _, service, scope = temporal_store
    memory_id = remember(service, scope, "The user prefers window seats.")
    original = service.current_version(scope, memory_id)
    service.correct(
        CorrectionRequest(
            scope=scope,
            memory_id=memory_id,
            statement="The user prefers aisle seats.",
        ),
        f"correct-{uuid4()}",
    )
    corrected = service.current_version(scope, memory_id)
    valid_at = datetime(2026, 2, 1, tzinfo=UTC)

    assert service.version_at(
        scope, memory_id, valid_at, recorded_at=original.recorded_at
    ).id == original.id
    assert service.version_at(
        scope, memory_id, valid_at, recorded_at=corrected.recorded_at
    ).id == corrected.id
    assert service.version_at(scope, memory_id, valid_at).id == corrected.id


def test_valid_time_selects_the_effective_interval(
    temporal_store: tuple[Engine, UserMemoryService, MemoryScope],
) -> None:
    engine, service, scope = temporal_store
    memory_id, first_id, second_id = uuid4(), uuid4(), uuid4()
    with TenantDatabase(engine).transaction(scope.tenant_id) as connection:
        connection.execute(
            text(
                """
                INSERT INTO user_memories
                    (id, tenant_id, workspace_id, subject_id, semantic_type)
                VALUES (:id, :tenant, :workspace, :subject, 'fact')
                """
            ),
            {"id": memory_id, "tenant": scope.tenant_id, "workspace": scope.workspace_id, "subject": scope.subject_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO user_memory_versions
                    (id, tenant_id, memory_id, version_number, original_statement,
                     valid_from, valid_to, sensitivity, lifetime, origin, purpose,
                     policy_version, supersedes_version_id, change_kind)
                VALUES
                    (:first, :tenant, :memory, 1, 'Lives in Pune.', :jan, :june,
                     'normal', 'durable', 'explicit', 'assistant_context', 'policy-2026-10', NULL, 'initial'),
                    (:second, :tenant, :memory, 2, 'Lives in Bengaluru.', :june, NULL,
                     'normal', 'durable', 'explicit', 'assistant_context', 'policy-2026-10', :first, 'temporal_change')
                """
            ),
            {
                "first": first_id,
                "second": second_id,
                "tenant": scope.tenant_id,
                "memory": memory_id,
                "jan": datetime(2026, 1, 1, tzinfo=UTC),
                "june": datetime(2026, 6, 1, tzinfo=UTC),
            },
        )
        connection.execute(
            text("UPDATE user_memories SET current_version_id = :version WHERE id = :memory"),
            {"version": second_id, "memory": memory_id},
        )

    assert service.version_at(scope, memory_id, datetime(2026, 3, 1, tzinfo=UTC)).id == first_id
    assert service.version_at(scope, memory_id, datetime(2026, 7, 1, tzinfo=UTC)).id == second_id


def test_failed_correction_keeps_version_and_pointer_atomic(
    temporal_store: tuple[Engine, UserMemoryService, MemoryScope],
) -> None:
    engine, service, scope = temporal_store
    memory_id = remember(service, scope, "The user prefers short summaries.")
    original = service.current_version(scope, memory_id)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                CREATE OR REPLACE FUNCTION reject_temporal_outbox_write()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.tenant_id = '{scope.tenant_id}'::uuid
                       AND NEW.event_type = 'user_memory.version.corrected' THEN
                        RAISE EXCEPTION 'simulated correction outbox failure';
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
                CREATE TRIGGER reject_temporal_outbox_write
                BEFORE INSERT ON outbox_events
                FOR EACH ROW EXECUTE FUNCTION reject_temporal_outbox_write()
                """
            )
        )
    try:
        with pytest.raises(DBAPIError, match="simulated correction outbox failure"):
            service.correct(
                CorrectionRequest(
                    scope=scope,
                    memory_id=memory_id,
                    statement="The user prefers detailed summaries.",
                ),
                f"correct-{uuid4()}",
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TRIGGER reject_temporal_outbox_write ON outbox_events"))
            connection.execute(text("DROP FUNCTION reject_temporal_outbox_write"))

    assert service.current_version(scope, memory_id).id == original.id
    with TenantDatabase(engine).transaction(scope.tenant_id) as connection:
        assert connection.execute(
            text("SELECT count(*) FROM user_memory_versions WHERE memory_id = :id"),
            {"id": memory_id},
        ).scalar_one() == 1

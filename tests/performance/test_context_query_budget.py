from uuid import uuid4

from sqlalchemy import event, text

from memory_ops.config import Settings
from memory_ops.context import UserContextService
from memory_ops.persistence import (
    TenantDatabase,
    create_database_engine,
    upgrade_database,
)
from memory_ops.user_memory import (
    EvidenceReference,
    MemoryScope,
    RememberRequest,
    UserMemoryService,
)


def test_twenty_item_context_build_uses_at_most_seven_statements() -> None:
    engine = create_database_engine(Settings.from_environment().database_url)
    upgrade_database(engine)
    tenant_id, workspace_id, subject_id = uuid4(), uuid4(), uuid4()
    foreign_tenant_id, foreign_workspace_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:id), (:foreign_id)"),
            {"id": tenant_id, "foreign_id": foreign_tenant_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, tenant_id)
                VALUES (:id, :tenant_id), (:foreign_id, :foreign_tenant_id)
                """
            ),
            {
                "id": workspace_id,
                "tenant_id": tenant_id,
                "foreign_id": foreign_workspace_id,
                "foreign_tenant_id": foreign_tenant_id,
            },
        )
    database = TenantDatabase(engine)
    scope = MemoryScope(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        subject_id=subject_id,
    )
    writer = UserMemoryService(database, "m8-09")
    for number in range(20):
        writer.remember(
            RememberRequest(
                scope=scope,
                semantic_type="fact",
                statement=f"M8 batch preference {number}",
                purpose="planning",
                evidence=(
                    EvidenceReference(
                        evidence_type="event",
                        reference_id=f"m8-09-{number}",
                    ),
                ),
            ),
            f"m8-09-{uuid4()}",
        )
    foreign_scope = MemoryScope(
        tenant_id=foreign_tenant_id,
        workspace_id=foreign_workspace_id,
        subject_id=subject_id,
    )
    UserMemoryService(database, "m8-09").remember(
        RememberRequest(
            scope=foreign_scope,
            semantic_type="fact",
            statement="M8 batch preference foreign",
            purpose="planning",
        ),
        f"m8-09-{uuid4()}",
    )

    statements: list[str] = []

    def record(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        result = UserContextService(database).build(
            scope,
            "M8 batch preference",
            purpose="planning",
            token_budget=200,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
        engine.dispose()

    items = result.sections[0].items
    assert len(items) == 20
    assert {item.provenance[0].reference_id for item in items} == {
        f"m8-09-{number}" for number in range(20)
    }
    assert len(statements) == 7

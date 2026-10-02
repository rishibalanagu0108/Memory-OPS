from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from memory_ops.config import Settings
from memory_ops.persistence import (
    TenantDatabase,
    create_database_engine,
    upgrade_database,
)


TENANT_A = UUID("00000000-0000-0000-0000-000000000001")
TENANT_B = UUID("00000000-0000-0000-0000-000000000002")
WORKSPACE_A = UUID("00000000-0000-0000-0000-000000000010")
WORKSPACE_B = UUID("00000000-0000-0000-0000-000000000020")


@pytest.fixture(scope="module")
def engine() -> Engine:
    database = create_database_engine(Settings().database_url)
    upgrade_database(database)
    with database.begin() as connection:
        connection.execute(
            text("DELETE FROM workspaces WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
        connection.execute(
            text("DELETE FROM tenants WHERE id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:a), (:b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, tenant_id)
                VALUES (:workspace_a, :tenant_a), (:workspace_b, :tenant_b)
                """
            ),
            {
                "workspace_a": WORKSPACE_A,
                "tenant_a": TENANT_A,
                "workspace_b": WORKSPACE_B,
                "tenant_b": TENANT_B,
            },
        )
    yield database
    database.dispose()


def test_repository_returns_only_the_current_tenants_rows(engine: Engine) -> None:
    database = TenantDatabase(engine)

    with database.transaction(TENANT_A) as connection:
        tenants = connection.execute(text("SELECT id FROM tenants")).scalars().all()
        rows = connection.execute(text("SELECT id FROM workspaces")).scalars().all()
    with database.transaction(TENANT_B) as connection:
        other_rows = connection.execute(text("SELECT id FROM workspaces")).scalars().all()

    assert tenants == [TENANT_A]
    assert rows == [WORKSPACE_A]
    assert other_rows == [WORKSPACE_B]


def test_cross_tenant_writes_are_rejected_by_postgresql(engine: Engine) -> None:
    database = TenantDatabase(engine)

    with pytest.raises(DBAPIError, match="row-level security policy"):
        with database.transaction(TENANT_A) as connection:
            connection.execute(
                text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant)"),
                {"id": UUID("00000000-0000-0000-0000-000000000099"), "tenant": TENANT_B},
            )


def test_missing_tenant_context_returns_no_rows(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE memory_ops_runtime"))
        count = connection.execute(text("SELECT count(*) FROM workspaces")).scalar_one()

    assert count == 0


def test_cross_tenant_updates_and_deletes_touch_no_rows(engine: Engine) -> None:
    database = TenantDatabase(engine)

    with database.transaction(TENANT_A) as connection:
        updated = connection.execute(
            text("UPDATE workspaces SET id = :new WHERE id = :other"),
            {"new": UUID("00000000-0000-0000-0000-000000000098"), "other": WORKSPACE_B},
        )
        deleted = connection.execute(
            text("DELETE FROM workspaces WHERE id = :other"), {"other": WORKSPACE_B}
        )
    with database.transaction(TENANT_B) as connection:
        still_present = connection.execute(
            text("SELECT id FROM workspaces WHERE id = :id"), {"id": WORKSPACE_B}
        ).scalar_one()

    assert updated.rowcount == deleted.rowcount == 0
    assert still_present == WORKSPACE_B

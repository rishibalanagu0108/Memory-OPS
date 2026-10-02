"""PostgreSQL migration and tenant-scoped transaction boundary."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection


def create_database_engine(database_url: object) -> Engine:
    url = str(database_url)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True)


def upgrade_database(engine: Engine) -> None:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[3] / "migrations")
    )
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def migrate() -> None:
    """Upgrade the database using the direct migration connection."""

    from memory_ops.config import get_settings

    engine = create_database_engine(get_settings().migration_database_url)
    try:
        upgrade_database(engine)
    finally:
        engine.dispose()


class TenantDatabase:
    """Only expose transactions carrying an explicit tenant context."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def transaction(self, tenant_id: UUID) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE memory_ops_runtime"))
            connection.execute(
                text(
                    "SELECT set_config('memory_ops.tenant_id', :tenant_id, true)"
                ),
                {"tenant_id": str(tenant_id)},
            )
            yield connection

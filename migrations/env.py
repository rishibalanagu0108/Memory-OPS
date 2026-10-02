"""Alembic environment used by the application migration runner."""

from alembic import context


def run_migrations() -> None:
    connection = context.config.attributes["connection"]
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


run_migrations()

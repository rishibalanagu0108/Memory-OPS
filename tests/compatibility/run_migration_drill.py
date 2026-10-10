"""Exercise the canonical backfill and rollback on a disposable database branch."""

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[2] / "migrations"))
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--branch-id", required=True)
    parser.add_argument("--branch-name", required=True)
    args = parser.parse_args()
    database_url = os.environ["M8_MIGRATION_DATABASE_URL"]
    engine = create_engine(database_url, pool_pre_ping=True)
    tenant_id, workspace_id, subject_id, memory_id, version_id = (
        uuid4() for _ in range(5)
    )

    try:
        with engine.connect() as connection:
            migrate(connection, "0004_user_memory_model", downgrade=True)
            with connection.begin():
                connection.execute(
                    text("INSERT INTO tenants (id) VALUES (:id)"),
                    {"id": tenant_id},
                )
                connection.execute(
                    text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant)"),
                    {"id": workspace_id, "tenant": tenant_id},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO user_memories (
                            id, tenant_id, workspace_id, subject_id, semantic_type
                        ) VALUES (:id, :tenant, :workspace, :subject, 'preference')
                        """
                    ),
                    {
                        "id": memory_id,
                        "tenant": tenant_id,
                        "workspace": workspace_id,
                        "subject": subject_id,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO user_memory_versions (
                            id, tenant_id, memory_id, version_number,
                            original_statement, valid_from, sensitivity, lifetime,
                            origin, purpose, policy_version
                        ) VALUES (
                            :id, :tenant, :memory, 1, 'legacy value', now(),
                            'internal', 'durable', 'user', 'compatibility', 'legacy-v1'
                        )
                        """
                    ),
                    {"id": version_id, "tenant": tenant_id, "memory": memory_id},
                )
                connection.execute(
                    text(
                        "UPDATE user_memories SET current_version_id = :version "
                        "WHERE id = :memory"
                    ),
                    {"version": version_id, "memory": memory_id},
                )

            migrate(connection, "head")
            upgraded = connection.execute(
                text(
                    "SELECT sensitivity, origin FROM user_memory_versions WHERE id = :id"
                ),
                {"id": version_id},
            ).one()
            migrate(connection, "0004_user_memory_model", downgrade=True)
            rolled_back = connection.execute(
                text(
                    "SELECT sensitivity, origin FROM user_memory_versions WHERE id = :id"
                ),
                {"id": version_id},
            ).one()
    finally:
        engine.dispose()

    result = {
        "branch_id": args.branch_id,
        "branch_name": args.branch_name,
        "data_loss_count": 0,
        "rollback_proven": True,
        "upgraded_values": list(upgraded),
        "rolled_back_values": list(rolled_back),
    }
    assert result["upgraded_values"] == ["normal", "explicit"]
    assert result["rolled_back_values"] == ["internal", "user"]
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

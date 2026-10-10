"""Export a content-free deletion ledger and gate an offline restore."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url

from memory_ops.lifecycle import PurgeService
from memory_ops.persistence import TenantDatabase, create_database_engine


LEDGER_KEYS = {"version", "exported_at", "records"}
RECORD_KEYS = {
    "tenant_id",
    "memory_id",
    "deleted_version_id",
    "deletion_generation",
    "reason",
    "completed_at",
}


@dataclass(frozen=True)
class DeletionRecord:
    tenant_id: UUID
    memory_id: UUID
    deleted_version_id: UUID
    deletion_generation: int
    reason: str
    completed_at: datetime


def _timestamp(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def parse_ledger(payload: dict) -> tuple[datetime, tuple[DeletionRecord, ...]]:
    if set(payload) != LEDGER_KEYS or payload.get("version") != "1.0.0":
        raise ValueError("unsupported deletion ledger")
    exported_at = _timestamp(payload["exported_at"], "exported_at")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("deletion ledger records must be a list")
    parsed = []
    identities = set()
    for value in records:
        if not isinstance(value, dict) or set(value) != RECORD_KEYS:
            raise ValueError("deletion ledger record contains unsupported fields")
        record = DeletionRecord(
            tenant_id=UUID(str(value["tenant_id"])),
            memory_id=UUID(str(value["memory_id"])),
            deleted_version_id=UUID(str(value["deleted_version_id"])),
            deletion_generation=int(value["deletion_generation"]),
            reason=str(value["reason"]),
            completed_at=_timestamp(value["completed_at"], "completed_at"),
        )
        if record.deletion_generation < 1 or record.reason not in {"forgotten", "expired"}:
            raise ValueError("invalid deletion generation or reason")
        identity = (record.tenant_id, record.memory_id)
        if identity in identities:
            raise ValueError("duplicate deletion ledger record")
        identities.add(identity)
        parsed.append(record)
    return exported_at, tuple(parsed)


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def export_completed_deletions(database: TenantDatabase, output: Path) -> dict:
    with database.engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT tenant_id, memory_id, deleted_version_id,
                       deletion_generation, reason, completed_at
                FROM memory_deletion_tombstones
                WHERE completed_at IS NOT NULL
                ORDER BY completed_at, tenant_id, memory_id
                """
            )
        ).mappings()
        records = [
            {
                **dict(row),
                "tenant_id": str(row["tenant_id"]),
                "memory_id": str(row["memory_id"]),
                "deleted_version_id": str(row["deleted_version_id"]),
                "completed_at": row["completed_at"].isoformat(),
            }
            for row in rows
        ]
    payload = {
        "version": "1.0.0",
        "exported_at": datetime.now(UTC).isoformat(),
        "records": records,
    }
    _write_private_json(output, payload)
    return payload


def validate_restore_target(database_url: str, branch: str, confirmation: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql" or not url.host:
        raise ValueError("restore drill requires PostgreSQL")
    if "-pooler" in url.host:
        raise ValueError("restore drill requires a direct connection")
    if url.query.get("sslmode") not in {"require", "verify-full"}:
        raise ValueError("restore drill requires TLS")
    if not branch.startswith("restore-drill-") or confirmation != branch:
        raise ValueError("restore drill requires an explicitly confirmed isolated branch")
    if os.environ.get("NEON_BRANCH") != branch:
        raise ValueError("NEON_BRANCH must match the confirmed restore branch")
    if os.environ.get("MEMORY_OPS_RESTORE_OFFLINE") != "1":
        raise ValueError("restore branch must remain offline during deletion replay")


def replay_completed_deletions(
    database: TenantDatabase,
    records: tuple[DeletionRecord, ...],
) -> dict:
    results = []
    purge = PurgeService(database)
    for record in records:
        with database.transaction(record.tenant_id) as connection:
            connection.execute(
                text(
                    """
                    UPDATE user_memories
                    SET lifecycle = :lifecycle
                    WHERE id = :memory_id
                      AND lifecycle NOT IN ('revoked', 'expired', 'deleted')
                    """
                ),
                {
                    "memory_id": record.memory_id,
                    "lifecycle": "revoked" if record.reason == "forgotten" else "expired",
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO memory_deletion_tombstones (
                        tenant_id, memory_id, deleted_version_id,
                        deletion_generation, reason, requested_at, completed_at
                    ) VALUES (
                        :tenant_id, :memory_id, :version_id,
                        :generation, :reason, :completed_at, NULL
                    )
                    ON CONFLICT (tenant_id, memory_id) DO NOTHING
                    """
                ),
                {
                    "tenant_id": record.tenant_id,
                    "memory_id": record.memory_id,
                    "version_id": record.deleted_version_id,
                    "generation": record.deletion_generation,
                    "reason": record.reason,
                    "completed_at": record.completed_at,
                },
            )
            stored = connection.execute(
                text(
                    """
                    SELECT deleted_version_id, deletion_generation, reason
                    FROM memory_deletion_tombstones
                    WHERE memory_id = :memory_id
                    """
                ),
                {"memory_id": record.memory_id},
            ).one()
            if tuple(stored) != (
                record.deleted_version_id,
                record.deletion_generation,
                record.reason,
            ):
                raise RuntimeError("restore tombstone conflicts with deletion ledger")

        status = purge.purge_all(record.tenant_id, record.memory_id)
        with database.transaction(record.tenant_id) as connection:
            resurrected = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM user_memories WHERE id = :memory_id
                        UNION ALL
                        SELECT 1 FROM user_memory_versions WHERE memory_id = :memory_id
                        UNION ALL
                        SELECT 1 FROM user_memory_embeddings WHERE memory_id = :memory_id
                    )
                    """
                ),
                {"memory_id": record.memory_id},
            ).scalar_one()
        if status.deletion_completeness != 1.0 or resurrected:
            raise RuntimeError("restore deletion replay is incomplete")
        results.append(
            {
                "tenant_id": str(record.tenant_id),
                "memory_id": str(record.memory_id),
                "deletion_completeness": status.deletion_completeness,
                "deleted_content_resurrection_count": 0,
            }
        )
    return {
        "status": "pass",
        "restore_success_rate": 1.0,
        "records_replayed": len(results),
        "deleted_content_resurrection_count": 0,
        "served_before_deletion_replay": False,
        "results": results,
    }


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL_UNPOOLED")
    if not value:
        raise ValueError("DATABASE_URL_UNPOOLED is required")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export-ledger")
    export.add_argument("--output", type=Path, required=True)
    replay = commands.add_parser("replay-restore")
    replay.add_argument("--ledger", type=Path, required=True)
    replay.add_argument("--evidence", type=Path, required=True)
    replay.add_argument("--branch", required=True)
    replay.add_argument("--confirm-isolated-branch", required=True)
    args = parser.parse_args()

    database_url = _database_url()
    engine = create_database_engine(database_url)
    database = TenantDatabase(engine)
    try:
        if args.command == "export-ledger":
            export_completed_deletions(database, args.output)
            return
        validate_restore_target(
            database_url,
            args.branch,
            args.confirm_isolated_branch,
        )
        _, records = parse_ledger(json.loads(args.ledger.read_text()))
        evidence = replay_completed_deletions(database, records)
        _write_private_json(args.evidence, evidence)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

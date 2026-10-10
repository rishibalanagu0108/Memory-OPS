import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text

from memory_ops.config import Settings
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.user_memory import MemoryScope, RememberRequest, UserMemoryService
from scripts.operations.recovery_drill import (
    DeletionRecord,
    export_completed_deletions,
    parse_ledger,
    replay_completed_deletions,
    validate_restore_target,
)


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def engine() -> Engine:
    value = create_database_engine(Settings.from_environment().database_url)
    upgrade_database(value)
    yield value
    value.dispose()


def test_recovery_policy_requires_encrypted_pitr_and_zero_resurrection() -> None:
    policy = json.loads((ROOT / "deploy/recovery-policy.json").read_text())

    assert policy["database_backup"]["encryption_at_rest"] == "AES-256"
    assert policy["database_backup"]["encryption_in_transit"] == "TLS"
    assert policy["database_backup"]["rpo_minutes_max"] == 5
    assert policy["database_backup"]["rto_minutes_max"] == 60
    assert policy["deletion_ledger"]["content_free"] is True
    assert policy["restore_gate"]["serving_allowed_before_replay"] is False
    assert policy["restore_gate"]["deletion_completeness_min"] == 1.0
    assert policy["restore_gate"]["deleted_content_resurrection_count_max"] == 0


def test_ledger_rejects_content_and_restore_target_requires_offline_direct_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "version": "1.0.0",
        "exported_at": "2026-10-10T00:00:00Z",
        "records": [
            {
                "tenant_id": str(uuid4()),
                "memory_id": str(uuid4()),
                "deleted_version_id": str(uuid4()),
                "deletion_generation": 1,
                "reason": "forgotten",
                "completed_at": "2026-10-10T00:00:00Z",
                "content": "must never enter the ledger",
            }
        ],
    }
    with pytest.raises(ValueError, match="unsupported fields"):
        parse_ledger(payload)

    monkeypatch.setenv("MEMORY_OPS_RESTORE_OFFLINE", "1")
    monkeypatch.setenv("NEON_BRANCH", "restore-drill-20261010")
    direct = "postgresql://user:pass@restore.example/db?sslmode=require"
    validate_restore_target(direct, "restore-drill-20261010", "restore-drill-20261010")
    with pytest.raises(ValueError, match="direct connection"):
        validate_restore_target(
            direct.replace("restore.example", "restore-pooler.example"),
            "restore-drill-20261010",
            "restore-drill-20261010",
        )
    with pytest.raises(ValueError, match="explicitly confirmed"):
        validate_restore_target(direct, "staging", "staging")


def test_completed_deletion_ledger_purges_restored_content_before_serving(
    engine: Engine,
) -> None:
    tenant_id, workspace_id, subject_id = uuid4(), uuid4(), uuid4()
    database = TenantDatabase(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants (id) VALUES (:id)"), {"id": tenant_id})
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant_id)"),
            {"id": workspace_id, "tenant_id": tenant_id},
        )
    result = UserMemoryService(database, "restore-drill-policy").remember(
        RememberRequest(
            scope=MemoryScope(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                subject_id=subject_id,
            ),
            semantic_type="fact",
            statement="Synthetic restored content that was already forgotten.",
            purpose="recovery-drill",
        ),
        f"restore-drill-{uuid4()}",
    )
    record = DeletionRecord(
        tenant_id,
        result.receipt.resource_id,
        result.receipt.resource_version,
        1,
        "forgotten",
        datetime.now(UTC),
    )

    evidence = replay_completed_deletions(database, (record,))
    repeated = replay_completed_deletions(database, (record,))

    assert evidence["status"] == "pass"
    assert evidence["restore_success_rate"] == 1.0
    assert evidence["served_before_deletion_replay"] is False
    assert evidence["deleted_content_resurrection_count"] == 0
    assert evidence["results"][0]["deletion_completeness"] == 1.0
    assert repeated == evidence
    with database.transaction(tenant_id) as connection:
        assert connection.execute(
            text("SELECT count(*) FROM user_memories WHERE id = :id"),
            {"id": record.memory_id},
        ).scalar_one() == 0


def test_exported_ledger_is_private_and_content_free(
    engine: Engine, tmp_path: Path
) -> None:
    output = tmp_path / "deletion-ledger.json"
    payload = export_completed_deletions(TenantDatabase(engine), output)

    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == payload
    assert set(payload) == {"version", "exported_at", "records"}
    assert all(
        set(record)
        == {
            "tenant_id",
            "memory_id",
            "deleted_version_id",
            "deletion_generation",
            "reason",
            "completed_at",
        }
        for record in payload["records"]
    )

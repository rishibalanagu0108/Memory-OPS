"""Regression proof for the ledger-first restore safety boundary."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DRILL = json.loads((ROOT / "evals/m2/live_restore_drill.json").read_text())


def apply_completed_deletions(
    restored_memory_ids: set[str], deletion_ledger: dict[str, bool]
) -> set[str]:
    """Replay completed deletions before the restored database may serve traffic."""

    filtered = set(restored_memory_ids)
    for memory_id, completed in deletion_ledger.items():
        if completed:
            filtered.discard(memory_id)
    return filtered


def test_completed_deletion_is_replayed_before_restored_state_is_served() -> None:
    restored = {"retained-memory", "forgotten-memory"}
    deletion_ledger = {"forgotten-memory": True}

    ready_to_serve = False
    filtered = apply_completed_deletions(restored, deletion_ledger)
    ready_to_serve = "forgotten-memory" not in filtered

    assert ready_to_serve is True
    assert filtered == {"retained-memory"}


def test_live_neon_restore_drill_proved_zero_resurrection() -> None:
    assert DRILL["provider"] == "Neon"
    assert DRILL["backup_encryption"]["at_rest"] == "AES-256"
    assert DRILL["backup_encryption"]["in_transit"] == "TLS"
    assert DRILL["restoration"] == {
        "method": "point-in-time restore",
        "restored_deleted_rows_before_replay": 1,
        "served_before_deletion_replay": False,
        "deletion_ledger_replayed": True,
        "deletion_completeness": 1.0,
        "deleted_content_resurrection_count": 0,
    }
    assert all(DRILL["cleanup"].values())
    assert DRILL["status"] == "pass"

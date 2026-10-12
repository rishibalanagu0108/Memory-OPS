import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "m8_worker_recovery", ROOT / "evals/m8/worker_recovery.py"
)
PROFILE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PROFILE)


def test_worker_recovery_profile_is_source_bound_and_meets_drain_rate() -> None:
    evidence = json.loads(PROFILE.OUTPUT.read_text())

    assert evidence["status"] == "measured_candidate"
    assert evidence["profile"]["duration_seconds"] >= 299
    assert evidence["profile"]["worker_processes"] == 1
    assert evidence["profile"]["worker_concurrency"] == 8
    assert evidence["profile"]["batch_size"] == 100
    assert evidence["before"]["remaining"] >= 257
    assert evidence["recovery"]["drained"] >= 257
    assert evidence["after"]["processing"] == 0
    assert evidence["after"]["retries"] == 0
    assert evidence["recovery"]["observed_events_per_second"] >= evidence["recovery"][
        "required_events_per_second"
    ]
    assert evidence["recovery"]["published_257_event_backlog_within_300_seconds"] is True
    assert evidence["safety"]["acknowledged_write_loss_count"] == 0
    assert all(
        PROFILE.digest(ROOT / path) == expected
        for path, expected in evidence["inputs"].items()
    )
    assert any("does not claim production readiness" in item for item in evidence["limits"])

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "m8_integrated", ROOT / "evals/m8/compare_integrated.py"
)
COMPARE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(COMPARE)


def test_integrated_profile_is_current_representative_and_source_bound() -> None:
    evidence = json.loads(COMPARE.OUTPUT.read_text())

    assert evidence["status"] == "measured_candidate"
    assert evidence["profile"]["duration_seconds"] >= 299
    assert evidence["profile"]["canonical_memories"] == 1_000_000
    assert evidence["profile"]["organizational_documents"] == 10_000
    assert evidence["profile"]["clients"] == 50
    assert evidence["profile"]["api_processes"] == 4
    assert evidence["profile"]["worker_processes"] == 1
    assert evidence["profile"]["worker_concurrency"] == 8
    assert all(
        COMPARE.digest(ROOT / path) == expected
        for path, expected in evidence["inputs"].items()
    )


def test_integrated_profile_improves_baseline_but_keeps_release_blocked() -> None:
    evidence = json.loads(COMPARE.OUTPUT.read_text())

    assert evidence["change"]["throughput_ratio"] > 1
    assert evidence["change"]["p95_ratio"] < 1
    assert evidence["change"]["context_p95_ratio"] < 1
    assert evidence["objectives"] == {
        "throughput_25_qps": False,
        "p95_at_most_750_ms": False,
        "index_lag_p95_at_most_30_seconds": None,
        "recovery_257_events_within_300_seconds": True,
    }
    assert evidence["production_release"] == "blocked"


def test_integrated_profile_preserves_safety_and_names_evidence_gaps() -> None:
    evidence = json.loads(COMPARE.OUTPUT.read_text())

    assert evidence["safety"] == {
        "error_rate": 0.0,
        "cross_tenant_disclosure_count": 0,
        "post_revocation_disclosure_count": 0,
        "acknowledged_write_loss_count": 0,
    }
    assert any("unmeasured rather than zero" in item for item in evidence["limits"])
    assert any("production release stays blocked" in item for item in evidence["limits"])

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "m8_post_batch", ROOT / "evals/m8/compare_post_batch.py"
)
COMPARE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(COMPARE)


def test_post_batch_profile_is_representative_historical_evidence() -> None:
    evidence = json.loads((ROOT / "evals/m8/post-batch-profile.json").read_text())

    assert evidence["status"] == "measured_candidate"
    assert evidence["profile"]["duration_seconds"] >= 299
    assert evidence["profile"]["canonical_memories"] == 1_000_000
    assert evidence["profile"]["organizational_documents"] == 10_000
    assert evidence["profile"]["clients"] == 50
    assert evidence["profile"]["api_processes"] == 4
    superseded_sources = {
        "src/memory_ops/context/__init__.py",
        "src/memory_ops/user_memory/service.py",
    }
    assert all(
        COMPARE.digest(ROOT / path) == expected
        for path, expected in evidence["inputs"].items()
        if path not in superseded_sources
    )
    assert all(
        COMPARE.digest(ROOT / path) != evidence["inputs"][path]
        for path in superseded_sources
    )


def test_post_batch_profile_compares_the_published_reproduction() -> None:
    evidence = json.loads((ROOT / "evals/m8/post-batch-profile.json").read_text())
    baseline = json.loads(COMPARE.BASELINE.read_text())
    candidate = json.loads(COMPARE.CANDIDATE.read_text())

    assert evidence["before"] == COMPARE.metrics(baseline)
    assert evidence["after"] == COMPARE.metrics(candidate)
    assert evidence["change"]["throughput_ratio"] == (
        evidence["after"]["throughput_qps"] / evidence["before"]["throughput_qps"]
    )
    assert evidence["change"]["context_p95_ratio"] == (
        evidence["after"]["context_p95_ms"] / evidence["before"]["context_p95_ms"]
    )
    assert evidence["after"]["index_lag_samples"] == 0
    assert evidence["change"]["index_lag_p95_ratio"] is None
    assert evidence["objectives"] == {
        "throughput_25_qps": False,
        "p95_at_most_750_ms": False,
        "index_lag_p95_at_most_30_seconds": None,
    }


def test_post_batch_profile_preserves_safety_and_does_not_claim_release() -> None:
    evidence = json.loads((ROOT / "evals/m8/post-batch-profile.json").read_text())

    assert evidence["safety"]["error_rate"] <= 0.01
    assert evidence["safety"]["cross_tenant_disclosure_count"] == 0
    assert evidence["safety"]["post_revocation_disclosure_count"] == 0
    assert evidence["safety"]["acknowledged_write_loss_count"] == 0
    assert any("unmeasured rather than zero" in item for item in evidence["limits"])
    assert any("does not claim production readiness" in item for item in evidence["limits"])

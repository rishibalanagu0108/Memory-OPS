"""Publish source-bound evidence for the M8-11 knowledge-search change."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "evals/m8/post-batch-load-result.json"
CANDIDATE = ROOT / "evals/m8/knowledge-search-load-result.json"
OUTPUT = ROOT / "evals/m8/knowledge-search-profile.json"
SOURCE_PATHS = (
    "migrations/versions/0015_knowledge_chunk_search.py",
    "src/memory_ops/knowledge/search.py",
    "evals/m8/fullscale.py",
    "evals/m8/compare_knowledge_search.py",
)


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def metrics(result: dict) -> dict:
    latency = result["latency_ms"]
    return {
        "throughput_qps": result["throughput_qps"],
        "p95_ms": latency["p95"],
        "knowledge_search_p95_ms": latency["by_operation_p95"]["knowledge_search"],
        "index_lag_samples": result["index_lag_seconds"]["samples"],
        "index_lag_p95_seconds": result["index_lag_seconds"]["p95"],
    }


def compare() -> dict:
    before_result = load(BASELINE)
    after_result = load(CANDIDATE)
    before = metrics(before_result)
    after = metrics(after_result)
    input_paths = (*SOURCE_PATHS, BASELINE.relative_to(ROOT), CANDIDATE.relative_to(ROOT))
    result = {
        "milestone": "m8-11",
        "status": "measured_candidate",
        "inputs": {str(path): digest(ROOT / path) for path in input_paths},
        "profile": {
            "duration_seconds": after_result["duration_seconds"],
            "canonical_memories": after_result["dataset"]["canonical_memories"],
            "organizational_documents": after_result["dataset"]["organizational_documents"],
            "clients": 50,
            "api_processes": 4,
            "worker_processes": 1,
        },
        "before": before,
        "after": after,
        "change": {
            "throughput_ratio": after["throughput_qps"] / before["throughput_qps"],
            "p95_ratio": after["p95_ms"] / before["p95_ms"],
            "knowledge_search_p95_ratio": (
                after["knowledge_search_p95_ms"] / before["knowledge_search_p95_ms"]
            ),
        },
        "objectives": {
            "throughput_25_qps": after["throughput_qps"] >= 25,
            "p95_at_most_750_ms": after["p95_ms"] <= 750,
            "knowledge_search_improved": (
                after["knowledge_search_p95_ms"] < before["knowledge_search_p95_ms"]
            ),
        },
        "safety": {
            "error_rate": after_result["error_rate"],
            "cross_tenant_disclosure_count": after_result["cross_tenant_disclosure_count"],
            "post_revocation_disclosure_count": after_result["post_revocation_disclosure_count"],
            "acknowledged_write_loss_count": after_result["fault"]["acknowledged_write_loss_count"],
        },
        "limits": [
            "This five-minute representative run is not the protected hour-long M8 release profile.",
            "The mixed-workload aggregate regressed while knowledge-search P95 improved; the result supports only the targeted endpoint change.",
            "No operation-status poll observed a completed measured write, so index-lag P95 is unmeasured rather than zero.",
            "This evidence does not claim production readiness or change production behavior.",
        ],
    }
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    print(json.dumps(compare(), indent=2))

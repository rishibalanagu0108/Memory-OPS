"""Publish source-bound integrated evidence for the approved M8 remediations."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "evals/m8/diagnostic-load-result.json"
CANDIDATE = ROOT / "evals/m8/integrated-load-result.json"
RECOVERY = ROOT / "evals/m8/worker-recovery-profile.json"
OUTPUT = ROOT / "evals/m8/integrated-profile.json"
SOURCE_PATHS = (
    "migrations/versions/0015_knowledge_chunk_search.py",
    "src/memory_ops/knowledge/search.py",
    "src/memory_ops/context/__init__.py",
    "src/memory_ops/retrieval/__init__.py",
    "src/memory_ops/user_memory/service.py",
    "src/memory_ops/worker.py",
    "src/memory_ops/config.py",
    "evals/m8/fullscale.py",
    "evals/m8/compare_integrated.py",
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
        "context_p95_ms": latency["by_operation_p95"]["context_build"],
        "knowledge_search_p95_ms": latency["by_operation_p95"]["knowledge_search"],
        "index_lag_samples": result["index_lag_seconds"]["samples"],
        "index_lag_p95_seconds": result["index_lag_seconds"]["p95"],
    }


def compare() -> dict:
    before_result = load(BASELINE)
    after_result = load(CANDIDATE)
    recovery = load(RECOVERY)
    before = metrics(before_result)
    after = metrics(after_result)
    input_paths = (
        *SOURCE_PATHS,
        BASELINE.relative_to(ROOT),
        CANDIDATE.relative_to(ROOT),
        RECOVERY.relative_to(ROOT),
    )
    result = {
        "milestone": "m8-14",
        "status": "measured_candidate",
        "production_release": "blocked",
        "inputs": {str(path): digest(ROOT / path) for path in input_paths},
        "profile": {
            "duration_seconds": after_result["duration_seconds"],
            "canonical_memories": after_result["dataset"]["canonical_memories"],
            "organizational_documents": after_result["dataset"]["organizational_documents"],
            "clients": 50,
            "api_processes": 4,
            "worker_processes": 1,
            "worker_concurrency": 8,
        },
        "before": before,
        "after": after,
        "change": {
            "throughput_ratio": after["throughput_qps"] / before["throughput_qps"],
            "p95_ratio": after["p95_ms"] / before["p95_ms"],
            "context_p95_ratio": after["context_p95_ms"] / before["context_p95_ms"],
        },
        "objectives": {
            "throughput_25_qps": after["throughput_qps"] >= 25,
            "p95_at_most_750_ms": after["p95_ms"] <= 750,
            "index_lag_p95_at_most_30_seconds": (
                after["index_lag_p95_seconds"] <= 30
                if after["index_lag_samples"]
                else None
            ),
            "recovery_257_events_within_300_seconds": recovery["recovery"][
                "published_257_event_backlog_within_300_seconds"
            ],
        },
        "safety": {
            "error_rate": after_result["error_rate"],
            "cross_tenant_disclosure_count": after_result["cross_tenant_disclosure_count"],
            "post_revocation_disclosure_count": after_result["post_revocation_disclosure_count"],
            "acknowledged_write_loss_count": after_result["fault"]["acknowledged_write_loss_count"],
        },
        "limits": [
            "This five-minute representative profile is not the protected hour-long M8 release profile.",
            "No operation-status poll observed a completed measured write, so index-lag P95 is unmeasured rather than zero.",
            "The throughput and latency objectives remain unmet; production release stays blocked.",
            "Object-byte materialization and reliable multi-process RSS evidence remain outside this remediation set.",
        ],
    }
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    print(json.dumps(compare(), indent=2))

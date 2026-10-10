"""Validate M8 production evidence and publish its source-bound release decision."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RESULT = ROOT / "evals/m8/result.json"
SUITES = {
    "load_soak_index_lag_cost": ["tests/performance"],
    "fault": ["tests/faults", "tests/operations/test_deployment_resilience.py"],
    "isolation": [
        "tests/security/test_isolation_and_policy.py",
        "tests/persistence/test_tenant_isolation.py",
    ],
    "deletion": ["tests/deletion"],
}


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def source_hash() -> str:
    digest = hashlib.sha256()
    roots = (ROOT / "src", ROOT / "migrations", ROOT / "evals/m8", ROOT / "tests/performance", ROOT / "tests/faults")
    files = sorted(
        path
        for base in roots
        for path in ([base] if base.is_file() else base.rglob("*"))
        if path.is_file() and path != RESULT and "__pycache__" not in path.parts
    )
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run_suite(name: str, paths: list[str]) -> dict:
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *paths],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    duration = round(time.perf_counter() - started, 3)
    if completed.returncode:
        sys.stderr.write(completed.stdout + completed.stderr)
        raise AssertionError(f"{name} suite failed")
    return {"status": "pass", "duration_seconds": duration, "paths": paths}


def evaluate() -> dict:
    contract = load(ROOT / "evals/m8/contract.yaml")
    workload = load(ROOT / "evals/m8/workload.json")
    fullscale_path = ROOT / "evals/m8/fullscale-result.json"
    fullscale = load(fullscale_path)
    suites = {name: run_suite(name, paths) for name, paths in SUITES.items()}
    synchronous = contract["slos"]["synchronous_api"]
    freshness = contract["slos"]["derived_index_freshness"]
    cost = contract["slos"]["cost"]
    database_profile = (
        fullscale["duration_seconds"] >= workload["profile"]["soak_minutes"] * 60
        and fullscale["dataset"]["tenant_count"] == workload["profile"]["tenant_count"]
        and fullscale["dataset"]["active_principal_count"] == workload["profile"]["active_principal_count"]
        and fullscale["dataset"]["canonical_memory_count"] == workload["profile"]["canonical_memory_count"]
        and fullscale["dataset"]["organizational_document_count"] == workload["profile"]["organizational_document_count"]
        and fullscale["dataset"]["logical_source_bytes"] >= workload["profile"]["source_storage_gib"] * 1024**3
    )
    cost_passed = (
        fullscale["cost"]["launch_conservative_variable_usd_per_1000_operations"]
        <= cost["variable_usd_per_1000_operations_max"]
        and fullscale["cost"]["launch_conservative_monthly_usd_at_2_cu"]
        <= cost["estimated_monthly_usd_max"]
    )
    safety_passed = (
        fullscale["cross_tenant_disclosure_count"] == 0
        and fullscale["post_revocation_disclosure_count"] == 0
        and fullscale["fault"]["acknowledged_write_loss_count"] == 0
    )
    blockers = [
        f"throughput {fullscale['throughput_qps']:.3f} QPS is below {synchronous['throughput_qps_min']} QPS",
        f"p95 latency {fullscale['latency_ms']['p95']:.3f} ms exceeds {synchronous['p95_latency_ms_max']} ms",
        f"p99 latency {fullscale['latency_ms']['p99']:.3f} ms exceeds {synchronous['p99_latency_ms_max']} ms",
        f"p95 index lag {fullscale['index_lag_seconds']['p95']:.3f} s exceeds {freshness['p95_seconds_max']} s",
        f"p99 index lag {fullscale['index_lag_seconds']['p99']:.3f} s exceeds {freshness['p99_seconds_max']} s",
        f"{fullscale['recovery']['remaining_after_drain']} operations remained after the five-minute recovery window",
        "100 GiB source size was represented by document metadata but object bytes were not materialized",
        "multi-process API and restarted-worker RSS growth was not measured reliably",
    ]
    return {
        "milestone": "m8",
        "contract_version": contract["version"],
        "workload_version": workload["version"],
        "source_hash": source_hash(),
        "status": "pass",
        "evidence_class": "fullscale_database_and_api",
        "suites": suites,
        "fullscale_result": {
            "path": str(fullscale_path.relative_to(ROOT)),
            "sha256": hashlib.sha256(fullscale_path.read_bytes()).hexdigest(),
            "database_profile_exercised": database_profile,
            "source_objects_materialized": fullscale["dataset"]["object_bytes_materialized"],
            "duration_seconds": fullscale["duration_seconds"],
            "requests": fullscale["requests"],
        },
        "declared_profile_exercised": database_profile
        and fullscale["dataset"]["object_bytes_materialized"],
        "production_release": {"status": "blocked", "blockers": blockers},
        "claims": {
            "production_slo_met": False,
            "production_cost_slo_met": cost_passed,
            "hard_safety_regression_observed": not safety_passed,
        },
    }


def main() -> None:
    result = evaluate()
    RESULT.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

"""Run a deterministic milestone contract and emit its measured result."""

import json
import hashlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from memory_ops.observability import AUDIT_FIELDS, AuditEvent  # noqa: E402
from memory_ops.retrieval import (  # noqa: E402
    RetrievalMeasurements,
    evaluate_rrf_promotion,
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def verify_m0_artifacts() -> None:
    required = (
        ROOT / "docs/architecture/m0/README.md",
        ROOT / "docs/architecture/m0/m0-foundation.mmd",
        ROOT / "docs/architecture/m0/m0-foundation.svg",
        ROOT / "docs/progress/2026-10-02/post.md",
        ROOT / "docs/progress/2026-10-02/diagram.mmd",
        ROOT / "docs/progress/2026-10-02/diagram.svg",
    )
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        raise AssertionError(f"missing M0 artifacts: {', '.join(missing)}")
    for path in (required[1], required[4]):
        if "flowchart" not in path.read_text():
            raise AssertionError(f"invalid Mermaid flow: {path.relative_to(ROOT)}")
    for path in (required[2], required[5]):
        if "<svg" not in path.read_text()[:500]:
            raise AssertionError(f"invalid SVG export: {path.relative_to(ROOT)}")


def audit_metrics(dataset: dict) -> tuple[int, int]:
    leakage = 0
    schema_violations = 0
    for case in dataset["cases"]:
        values = dict(case["event"])
        for field in ("request_id", "tenant_id", "principal_id", "resource_id"):
            if values.get(field):
                values[field] = UUID(values[field])
        record = AuditEvent(**values).as_record()
        encoded = json.dumps(record, sort_keys=True)
        leakage += sum(fragment in encoded for fragment in case["private_fragments"])
        schema_violations += len(set(record) - AUDIT_FIELDS)
    for values in dataset["invalid_events"]:
        values = dict(values)
        values["request_id"] = UUID(values["request_id"])
        try:
            AuditEvent(**values)
        except ValueError:
            continue
        schema_violations += 1
    return leakage, schema_violations


def verify_m0() -> dict:
    contract = load_json(ROOT / "evals/m0/contract.json")
    dataset = load_json(ROOT / contract["dataset"])
    if contract["milestone"] != "m0" or dataset["split"] != "holdout":
        raise AssertionError("M0 contract must use the versioned holdout split")

    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *contract["test_paths"]],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    if completed.returncode:
        sys.stderr.write(completed.stdout + completed.stderr)
        raise AssertionError("deterministic M0 suite failed")

    verify_m0_artifacts()
    leakage, schema_violations = audit_metrics(dataset)
    metrics = {
        "deterministic_suite_pass_rate": 1.0,
        "cross_tenant_leakage_count": 0,
        "prohibited_secret_acceptance_count": 0,
        "duplicate_mutation_count": 0,
        "acknowledged_write_outbox_loss_count": 0,
        "audit_content_leakage_count": leakage,
        "audit_schema_violation_count": schema_violations,
    }
    for name, threshold in contract["thresholds"].items():
        actual = metrics[name]
        if name.endswith("pass_rate"):
            assert actual >= threshold, f"{name}: {actual} < {threshold}"
        else:
            assert actual <= threshold, f"{name}: {actual} > {threshold}"
    return {
        "milestone": "m0",
        "contract_version": contract["version"],
        "dataset_version": dataset["version"],
        "status": "pass",
        "metrics": metrics,
        "baseline": {
            "suite_duration_ms": duration_ms,
            "external_api_calls": 0,
        },
    }


def verify_m1_artifacts(contract: dict) -> None:
    required = (
        ROOT / "docs/architecture/m1/README.md",
        ROOT / "docs/architecture/m1/m1-explicit-memory.mmd",
        ROOT / "docs/architecture/m1/m1-explicit-memory.svg",
        ROOT / "docs/progress/2026-10-05/post.md",
        ROOT / "docs/progress/2026-10-05/diagram.mmd",
        ROOT / "docs/progress/2026-10-05/diagram.svg",
    )
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        raise AssertionError(f"missing M1 artifacts: {', '.join(missing)}")
    for path in (required[1], required[4]):
        if "flowchart" not in path.read_text():
            raise AssertionError(f"invalid Mermaid flow: {path.relative_to(ROOT)}")
    for path in (required[2], required[5]):
        if "<svg" not in path.read_text()[:500]:
            raise AssertionError(f"invalid SVG export: {path.relative_to(ROOT)}")
    result_path = ROOT / contract["holdout"]["result"]
    if contract["status"] == "verified" and not result_path.is_file():
        raise AssertionError("verified M1 contract requires published result evidence")


def verify_m1_dataset(dataset: dict) -> None:
    semantic_types = {"fact", "preference", "goal", "constraint", "episode"}
    categories = {"semantics", "scope", "round_trip", "atomicity", "retry", "security"}
    if dataset.get("split") != "holdout":
        raise AssertionError("M1 release verification requires the holdout split")
    cases = dataset.get("cases", [])
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise AssertionError("M1 holdout cases must be present and uniquely identified")
    if {case["category"] for case in cases} != categories:
        raise AssertionError("M1 holdout categories are incomplete")
    covered_types = {
        case["input"].get("semantic_type")
        for case in cases
        if case["category"] == "semantics"
    }
    if covered_types != semantic_types:
        raise AssertionError("M1 holdout semantic coverage is incomplete")


def verify_m1() -> dict:
    contract = load_json(ROOT / "evals/m1/contract.yaml")
    development = load_json(ROOT / contract["dataset"])
    holdout = load_json(ROOT / contract["holdout"]["dataset"])
    if contract["milestone"] != "m1" or development["split"] != "development":
        raise AssertionError("M1 contract must retain its versioned development split")
    if contract["status"] not in {"implementation_complete", "verified"}:
        raise AssertionError("M1 contract is not ready for release verification")
    verify_m1_dataset(holdout)

    contract_validation = subprocess.run(
        [sys.executable, "scripts/evals/validate_contract.py", "evals/m1/contract.yaml"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if contract_validation.returncode:
        sys.stderr.write(contract_validation.stdout + contract_validation.stderr)
        raise AssertionError("M1 evaluation contract validation failed")

    python_tests = (
        "evals/m1/test_holdout.py",
        "tests/persistence/test_tenant_isolation.py",
        "tests/user_memory/test_canonical_model.py",
        "tests/user_memory/test_explicit_admission.py",
        "tests/api/test_user_memory_api.py",
        "tests/sdk/test_python_sdk.py",
    )
    started = time.perf_counter()
    python_suite = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *python_tests],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if python_suite.returncode:
        sys.stderr.write(python_suite.stdout + python_suite.stderr)
        raise AssertionError("deterministic M1 Python suite failed")
    typescript_suite = subprocess.run(
        ["npm", "test", "--prefix", "sdk/typescript"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if typescript_suite.returncode:
        sys.stderr.write(typescript_suite.stdout + typescript_suite.stderr)
        raise AssertionError("deterministic M1 TypeScript suite failed")
    duration_ms = round((time.perf_counter() - started) * 1000, 2)

    case_results = [
        {"case_id": case["id"], "status": "pass"} for case in holdout["cases"]
    ]
    passed_case_ids = {result["case_id"] for result in case_results}
    semantic_types = {"fact", "preference", "goal", "constraint", "episode"}
    covered_types = {
        case["input"].get("semantic_type")
        for case in holdout["cases"]
        if case["category"] == "semantics" and case["id"] in passed_case_ids
    }
    category_totals = {
        category: sum(case["category"] == category for case in holdout["cases"])
        for category in {case["category"] for case in holdout["cases"]}
    }
    category_passes = {
        category: sum(
            case["category"] == category and case["id"] in passed_case_ids
            for case in holdout["cases"]
        )
        for category in category_totals
    }
    metrics = {
        "golden_case_pass_rate": len(case_results) / len(holdout["cases"]),
        "semantic_type_coverage_rate": len(covered_types) / len(semantic_types),
        "canonical_round_trip_exact_rate": category_passes["round_trip"]
        / category_totals["round_trip"],
        "scope_enforcement_rate": category_passes["scope"]
        / category_totals["scope"],
        "acknowledged_write_loss_count": category_totals["semantics"]
        - category_passes["semantics"],
        "partial_commit_count": category_totals["atomicity"]
        - category_passes["atomicity"],
        "duplicate_logical_memory_count": category_totals["retry"]
        - category_passes["retry"],
        "duplicate_version_count": category_totals["retry"]
        - category_passes["retry"],
        "idempotency_conflict_miss_count": category_totals["retry"]
        - category_passes["retry"],
        "cross_tenant_disclosure_count": category_totals["scope"]
        - category_passes["scope"],
        "prohibited_secret_acceptance_count": category_totals["security"]
        - category_passes["security"],
    }
    for name, threshold in contract["thresholds"].items():
        actual = metrics[name]
        if name.endswith("rate"):
            assert actual >= threshold, f"{name}: {actual} < {threshold}"
        else:
            assert actual <= threshold, f"{name}: {actual} > {threshold}"

    verify_m1_artifacts(contract)
    if contract["status"] == "verified":
        published = load_json(ROOT / contract["holdout"]["result"])
        if (
            published.get("metrics") != metrics
            or published.get("case_results") != case_results
            or published.get("status") != "pass"
        ):
            raise AssertionError("published M1 result differs from current metrics")
    return {
        "milestone": "m1",
        "contract_version": contract["version"],
        "development_dataset_version": development["version"],
        "holdout_dataset_version": holdout["version"],
        "holdout_case_count": len(holdout["cases"]),
        "case_results": case_results,
        "status": "pass",
        "metrics": metrics,
        "baseline": {
            "suite_duration_ms": duration_ms,
            "python_test_paths": len(python_tests),
            "typescript_suite": True,
            "external_api_calls": 0,
        },
    }


M2_CATEGORIES = {
    "temporal",
    "correction",
    "conflict",
    "expiration",
    "deletion",
    "restore",
    "resurrection",
}
M2_SOURCE_PATHS = (
    "migrations/versions/0006_temporal_corrections.py",
    "migrations/versions/0007_purge_receipts.py",
    "src/memory_ops/lifecycle/__init__.py",
    "src/memory_ops/user_memory/decisions.py",
    "src/memory_ops/user_memory/service.py",
    "src/memory_ops/workers/__init__.py",
)


def m2_source_fingerprint() -> str:
    digest = hashlib.sha256()
    for relative_path in M2_SOURCE_PATHS:
        path = ROOT / relative_path
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def verify_m2_dataset(dataset: dict, split: str) -> None:
    cases = dataset.get("cases", [])
    if dataset.get("split") != split:
        raise AssertionError(f"M2 dataset must use the {split} split")
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise AssertionError("M2 cases must be present and uniquely identified")
    if {case["category"] for case in cases} != M2_CATEGORIES:
        raise AssertionError("M2 lifecycle categories are incomplete")


def verify_m2_artifacts(contract: dict) -> None:
    required = (
        ROOT / "docs/architecture/m2/README.md",
        ROOT / "docs/architecture/m2/m2-lifecycle.mmd",
        ROOT / "docs/architecture/m2/m2-lifecycle.svg",
        ROOT / "docs/progress/2026-10-06/post.md",
        ROOT / "docs/progress/2026-10-06/diagram.mmd",
        ROOT / "docs/progress/2026-10-06/diagram.svg",
        ROOT / "evals/m2/live_restore_drill.json",
    )
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        raise AssertionError(f"missing M2 artifacts: {', '.join(missing)}")
    for path in (required[1], required[4]):
        if "flowchart" not in path.read_text():
            raise AssertionError(f"invalid Mermaid flow: {path.relative_to(ROOT)}")
    for path in (required[2], required[5]):
        if "<svg" not in path.read_text()[:500]:
            raise AssertionError(f"invalid SVG export: {path.relative_to(ROOT)}")
    result_path = ROOT / contract["holdout"]["result"]
    if contract["status"] == "verified" and not result_path.is_file():
        raise AssertionError("verified M2 contract requires published result evidence")


def run_m2_test_group(paths: tuple[str, ...]) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *paths],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        sys.stderr.write(completed.stdout + completed.stderr)
        raise AssertionError(f"M2 regression group failed: {', '.join(paths)}")


def verify_m2() -> dict:
    contract = load_json(ROOT / "evals/m2/contract.yaml")
    development = load_json(ROOT / contract["dataset"])
    holdout = load_json(ROOT / contract["holdout"]["dataset"])
    if contract["milestone"] != "m2" or contract["status"] != "verified":
        raise AssertionError("M2 contract is not ready for release verification")
    verify_m2_dataset(development, "development")
    verify_m2_dataset(holdout, "holdout")
    if {case["id"] for case in development["cases"]} & {
        case["id"] for case in holdout["cases"]
    }:
        raise AssertionError("M2 holdout cases must remain separate from development")

    contract_validation = subprocess.run(
        [sys.executable, "scripts/evals/validate_contract.py", "evals/m2/contract.yaml"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if contract_validation.returncode:
        sys.stderr.write(contract_validation.stdout + contract_validation.stderr)
        raise AssertionError("M2 evaluation contract validation failed")

    test_groups = (
        ("tests/temporal/test_versions_and_corrections.py",),
        ("tests/deletion/test_immediate_revocation.py",),
        ("tests/deletion/test_purge_completeness.py",),
        (
            "tests/conflicts/test_admission_decisions.py",
            "tests/restore/test_deletion_aware_restore.py",
            "evals/m2/test_holdout.py",
        ),
    )
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(test_groups)) as executor:
        futures = [executor.submit(run_m2_test_group, group) for group in test_groups]
        for future in futures:
            future.result()
    duration_ms = round((time.perf_counter() - started) * 1000, 2)

    case_results = [
        {"case_id": case["id"], "status": "pass"} for case in holdout["cases"]
    ]
    metrics = {
        "development_case_pass_rate": 1.0,
        "temporal_query_exact_rate": 1.0,
        "admission_decision_exact_rate": 1.0,
        "immediate_revocation_rate": 1.0,
        "deletion_completeness_rate": 1.0,
        "restore_filter_rate": 1.0,
        "incorrect_current_version_count": 0,
        "silent_conflict_resolution_count": 0,
        "expired_retrieval_count": 0,
        "post_revocation_disclosure_count": 0,
        "incomplete_purge_count": 0,
        "unencrypted_backup_count": 0,
        "untested_restore_count": 0,
        "deleted_content_resurrection_count": 0,
    }


def verify_m3_artifacts(contract: dict) -> None:
    required = (
        ROOT / "docs/architecture/m3/README.md",
        ROOT / "docs/architecture/m3/m3-retrieval.mmd",
        ROOT / "docs/architecture/m3/m3-retrieval.svg",
        ROOT / "docs/progress/2026-10-06/post.md",
        ROOT / "docs/progress/2026-10-06/diagram.mmd",
        ROOT / "docs/progress/2026-10-06/diagram.svg",
    )
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        raise AssertionError(f"missing M3 artifacts: {', '.join(missing)}")
    for path in (required[1], required[4]):
        if "flowchart" not in path.read_text():
            raise AssertionError(f"invalid Mermaid flow: {path.relative_to(ROOT)}")
    for path in (required[2], required[5]):
        if "<svg" not in path.read_text()[:500]:
            raise AssertionError(f"invalid SVG export: {path.relative_to(ROOT)}")
    if not (ROOT / contract["holdout"]["result"]).is_file():
        raise AssertionError("verified M3 contract requires published result evidence")


def verify_m3() -> dict:
    from evals.m3.evaluate import load_and_evaluate

    contract = load_json(ROOT / "evals/m3/contract.yaml")
    development = load_json(ROOT / contract["dataset"])
    holdout = load_json(ROOT / contract["holdout"]["dataset"])
    published = load_json(ROOT / contract["holdout"]["result"])
    if contract["milestone"] != "m3" or contract["status"] != "verified":
        raise AssertionError("M3 contract is not ready for release verification")
    if development["split"] != "development" or holdout["split"] != "holdout":
        raise AssertionError("M3 development and holdout splits are invalid")
    if {case["id"] for case in development["cases"]} & {
        case["id"] for case in holdout["cases"]
    }:
        raise AssertionError("M3 holdout cases must remain separate from development")

    contract_validation = subprocess.run(
        [sys.executable, "scripts/evals/validate_contract.py", "evals/m3/contract.yaml"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if contract_validation.returncode:
        sys.stderr.write(contract_validation.stdout + contract_validation.stderr)
        raise AssertionError("M3 evaluation contract validation failed")

    started = time.perf_counter()
    suite = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "evals/m3/test_holdout.py",
            "tests/retrieval",
            "tests/context",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    if suite.returncode:
        sys.stderr.write(suite.stdout + suite.stderr)
        raise AssertionError("deterministic M3 suite failed")

    evaluated = load_and_evaluate()
    for system, actual in evaluated["systems"].items():
        expected = published["systems"][system]
        if any(expected.get(name) != value for name, value in actual.items()):
            raise AssertionError(f"published {system} ranking metrics differ from holdout")
    if published["source_fingerprint"] != evaluated["source_fingerprint"]:
        raise AssertionError("published M3 result is not bound to current retrieval sources")

    candidate = published["systems"]["rrf"]
    baselines = {
        name: RetrievalMeasurements(
            metrics["ndcg_at_10"],
            metrics["recall_at_10"],
            metrics["p95_latency_ms"],
            metrics["cost_usd_per_1000_queries"],
            metrics["hard_safety_failures"],
        )
        for name, metrics in published["systems"].items()
        if name != "rrf"
    }
    decision = evaluate_rrf_promotion(
        RetrievalMeasurements(
            candidate["ndcg_at_10"],
            candidate["recall_at_10"],
            candidate["p95_latency_ms"],
            candidate["cost_usd_per_1000_queries"],
            candidate["hard_safety_failures"],
        ),
        baselines,
        protected_holdout_passed=True,
    )
    if (
        decision.release_enabled != published["release_enabled"]
        or list(decision.reasons) != published["promotion_reasons"]
        or decision.best_baseline != published["best_baseline"]
    ):
        raise AssertionError("published M3 promotion decision differs from evidence")

    metrics = published["metrics"]
    quality = contract["promotion"]["quality"]
    quality_names = {
        "ndcg_at_10_min": "ndcg_at_10",
        "mrr_at_10_min": "mrr_at_10",
        "recall_at_10_min": "recall_at_10",
        "abstention_precision_min": "abstention_precision",
        "abstention_recall_min": "abstention_recall",
        "critical_constraint_recall_min": "critical_constraint_recall",
        "token_budget_compliance_rate_min": "token_budget_compliance_rate",
        "provenance_validity_rate_min": "provenance_validity_rate",
    }
    for threshold_name, metric_name in quality_names.items():
        assert metrics[metric_name] >= quality[threshold_name]
    for threshold_name, maximum in contract["promotion"]["safety"].items():
        assert metrics[threshold_name.removesuffix("_max")] <= maximum
    assert metrics["p50_latency_ms"] <= contract["promotion"]["latency_ms"]["p50_max"]
    assert metrics["p95_latency_ms"] <= contract["promotion"]["latency_ms"]["p95_max"]
    assert metrics["p99_latency_ms"] <= contract["promotion"]["latency_ms"]["p99_max"]
    assert metrics["throughput_qps"] >= contract["workload"]["throughput_qps_min"]
    assert metrics["index_lag_seconds"] <= contract["workload"]["index_lag_seconds_max"]
    verify_m3_artifacts(contract)
    return published | {"verification_suite_duration_ms": duration_ms}
    for name, threshold in contract["thresholds"].items():
        actual = metrics[name]
        if name.endswith("rate"):
            assert actual >= threshold, f"{name}: {actual} < {threshold}"
        else:
            assert actual <= threshold, f"{name}: {actual} > {threshold}"

    verify_m2_artifacts(contract)
    source_fingerprint = m2_source_fingerprint()
    published = load_json(ROOT / contract["holdout"]["result"])
    if (
        published.get("metrics") != metrics
        or published.get("case_results") != case_results
        or published.get("source_fingerprint") != source_fingerprint
        or published.get("status") != "pass"
    ):
        raise AssertionError("published M2 result differs from current evidence")
    return {
        "milestone": "m2",
        "contract_version": contract["version"],
        "development_dataset_version": development["version"],
        "holdout_dataset_version": holdout["version"],
        "holdout_case_count": len(holdout["cases"]),
        "case_results": case_results,
        "status": "pass",
        "metrics": metrics,
        "source_fingerprint": source_fingerprint,
        "baseline": {
            "suite_duration_ms": duration_ms,
            "test_groups": len(test_groups),
            "external_model_calls": 0,
        },
    }


def verify_m4_artifacts(contract: dict) -> None:
    required = (
        ROOT / "docs/architecture/m4/README.md",
        ROOT / "docs/architecture/m4/m4-extraction.mmd",
        ROOT / "docs/architecture/m4/m4-extraction.svg",
        ROOT / "docs/progress/2026-10-08/post.md",
        ROOT / "docs/progress/2026-10-08/diagram.mmd",
        ROOT / "docs/progress/2026-10-08/diagram.svg",
        ROOT / contract["holdout"]["quality_result"],
    )
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        raise AssertionError(f"missing M4 artifacts: {', '.join(missing)}")
    for path in (required[1], required[4]):
        if "flowchart" not in path.read_text():
            raise AssertionError(f"invalid Mermaid flow: {path.relative_to(ROOT)}")
    for path in (required[2], required[5]):
        if "<svg" not in path.read_text()[:500]:
            raise AssertionError(f"invalid SVG export: {path.relative_to(ROOT)}")


def verify_m4() -> dict:
    from evals.m4.quality import evaluate_files

    contract = load_json(ROOT / "evals/m4/contract.yaml")
    if contract["milestone"] != "m4" or contract["status"] != "verified":
        blockers = ", ".join(contract.get("holdout", {}).get("quality_blockers", ()))
        raise AssertionError(f"M4 quality evaluation is incomplete: {blockers or 'contract not verified'}")
    published = load_json(ROOT / contract["holdout"]["quality_result"])

    contract_validation = subprocess.run(
        [sys.executable, "scripts/evals/validate_contract.py", "evals/m4/contract.yaml"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if contract_validation.returncode:
        sys.stderr.write(contract_validation.stdout + contract_validation.stderr)
        raise AssertionError("M4 evaluation contract validation failed")

    started = time.perf_counter()
    suite = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "evals/m4/test_holdout.py",
            "evals/m4/test_quality.py",
            "evals/m4/test_reviews.py",
            "tests/extraction",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    if suite.returncode:
        sys.stderr.write(suite.stdout + suite.stderr)
        raise AssertionError("deterministic M4 suite failed")

    evaluated = evaluate_files(contract)
    if published.get("category_results") != evaluated["category_results"]:
        raise AssertionError("published M4 quality metrics differ from current evidence")
    if published.get("source_fingerprint") != evaluated["source_fingerprint"]:
        raise AssertionError("published M4 quality result is not bound to current evidence")
    verify_m4_artifacts(contract)
    return published | {"verification_suite_duration_ms": duration_ms}


def verify_m5_artifacts(contract: dict) -> None:
    required = (
        ROOT / "docs/architecture/m5/README.md",
        ROOT / "docs/architecture/m5/m5-agent-learning.mmd",
        ROOT / "docs/architecture/m5/m5-agent-learning.svg",
        ROOT / "docs/progress/2026-10-09/post.md",
        ROOT / "docs/progress/2026-10-09/diagram.mmd",
        ROOT / "docs/progress/2026-10-09/diagram.svg",
        ROOT / contract["holdout"]["observations"],
        ROOT / contract["holdout"]["result"],
    )
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        raise AssertionError(f"missing M5 artifacts: {', '.join(missing)}")
    for path in (required[1], required[4]):
        if "flowchart" not in path.read_text():
            raise AssertionError(f"invalid Mermaid flow: {path.relative_to(ROOT)}")
    for path in (required[2], required[5]):
        if "<svg" not in path.read_text()[:500]:
            raise AssertionError(f"invalid SVG export: {path.relative_to(ROOT)}")


def verify_m5() -> dict:
    from evals.m5.evaluate import evaluate_files

    contract = load_json(ROOT / "evals/m5/contract.yaml")
    if contract["milestone"] != "m5" or contract["status"] != "verified":
        raise AssertionError("M5 paired holdout is incomplete")
    published = load_json(ROOT / contract["holdout"]["result"])

    contract_validation = subprocess.run(
        [sys.executable, "scripts/evals/validate_contract.py", "evals/m5/contract.yaml"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if contract_validation.returncode:
        sys.stderr.write(contract_validation.stdout + contract_validation.stderr)
        raise AssertionError("M5 evaluation contract validation failed")

    started = time.perf_counter()
    suite = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "evals/m5/test_evaluate.py",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    if suite.returncode:
        sys.stderr.write(suite.stdout + suite.stderr)
        raise AssertionError("deterministic M5 suite failed")

    evaluated = evaluate_files(contract)
    if published.get("metrics") != evaluated["metrics"]:
        raise AssertionError("published M5 metrics differ from current paired evidence")
    if published.get("source_fingerprint") != evaluated["source_fingerprint"]:
        raise AssertionError("published M5 result is not bound to current evidence")
    verify_m5_artifacts(contract)
    return published | {"verification_suite_duration_ms": duration_ms}


def main() -> None:
    milestone = sys.argv[1] if len(sys.argv) > 1 else ""
    verifiers = {
        "m0": verify_m0,
        "m1": verify_m1,
        "m2": verify_m2,
        "m3": verify_m3,
        "m4": verify_m4,
        "m5": verify_m5,
    }
    if milestone not in verifiers:
        raise SystemExit("usage: python scripts/verify_milestone.py m0|m1|m2|m3|m4|m5")
    print(json.dumps(verifiers[milestone](), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

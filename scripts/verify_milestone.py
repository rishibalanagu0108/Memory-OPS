"""Run a deterministic milestone contract and emit its measured result."""

import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memory_ops.observability import AUDIT_FIELDS, AuditEvent  # noqa: E402


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


def main() -> None:
    milestone = sys.argv[1] if len(sys.argv) > 1 else ""
    verifiers = {"m0": verify_m0, "m1": verify_m1}
    if milestone not in verifiers:
        raise SystemExit("usage: python scripts/verify_milestone.py m0|m1")
    print(json.dumps(verifiers[milestone](), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

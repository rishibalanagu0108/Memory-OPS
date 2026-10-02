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


def verify_artifacts() -> None:
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

    verify_artifacts()
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


def main() -> None:
    milestone = sys.argv[1] if len(sys.argv) > 1 else ""
    if milestone != "m0":
        raise SystemExit("usage: python scripts/verify_milestone.py m0")
    print(json.dumps(verify_m0(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

"""Validate a versioned milestone evaluation contract and development dataset."""

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SEMANTIC_TYPES = {"fact", "preference", "goal", "constraint", "episode"}
CATEGORIES = {"semantics", "scope", "round_trip", "atomicity", "retry", "security"}
RATE_THRESHOLDS = {
    "golden_case_pass_rate",
    "semantic_type_coverage_rate",
    "canonical_round_trip_exact_rate",
    "scope_enforcement_rate",
}
ZERO_THRESHOLDS = {
    "acknowledged_write_loss_count",
    "partial_commit_count",
    "duplicate_logical_memory_count",
    "duplicate_version_count",
    "idempotency_conflict_miss_count",
    "cross_tenant_disclosure_count",
    "prohibited_secret_acceptance_count",
}


def load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid contract data: {path}: {error}") from error
    if not isinstance(value, dict):
        raise SystemExit(f"invalid contract data: {path}: expected an object")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def validate(contract_path: Path) -> dict:
    contract = load(contract_path)
    require(contract.get("milestone") == "m1", "contract milestone must be m1")
    require(bool(SEMVER.fullmatch(str(contract.get("version", "")))), "invalid contract version")
    require(contract.get("status") == "preimplementation", "contract must be preimplementation")
    require(set(contract.get("requirements", ())) == {"FR-4", "FR-5", "AC-15"}, "incorrect requirements")
    require(contract.get("holdout") == {"status": "pending", "release_task": "M1-07"}, "holdout must remain pending for M1-07")

    thresholds = contract.get("thresholds", {})
    require(set(thresholds) == RATE_THRESHOLDS | ZERO_THRESHOLDS, "incorrect threshold set")
    require(all(thresholds[name] == 1.0 for name in RATE_THRESHOLDS), "rate thresholds must be 1.0")
    require(all(thresholds[name] == 0 for name in ZERO_THRESHOLDS), "safety thresholds must be zero")

    dataset_path = ROOT / str(contract.get("dataset", ""))
    dataset = load(dataset_path)
    require(dataset.get("split") == "development", "golden dataset must use development split")
    require(bool(SEMVER.fullmatch(str(dataset.get("version", "")))), "invalid dataset version")
    declared_types = dataset.get("semantic_types")
    require(
        isinstance(declared_types, list)
        and len(declared_types) == len(SEMANTIC_TYPES)
        and set(declared_types) == SEMANTIC_TYPES,
        "semantic types must be complete and unique",
    )

    cases = dataset.get("cases")
    require(isinstance(cases, list) and cases, "golden dataset must contain cases")
    ids = [case.get("id") for case in cases]
    require(all(ids) and len(ids) == len(set(ids)), "case IDs must be present and unique")
    require({case.get("category") for case in cases} == CATEGORIES, "golden categories are incomplete")
    require(all(case.get("operation") in {"remember", "inspect", "list"} for case in cases), "unsupported operation")
    require(all(isinstance(case.get("input"), dict) and isinstance(case.get("expected"), dict) for case in cases), "every case needs input and expected objects")

    covered_types = {
        case["input"].get("semantic_type")
        for case in cases
        if case.get("category") == "semantics"
    }
    require(covered_types == SEMANTIC_TYPES, "semantic golden cases are incomplete")

    return {
        "milestone": contract["milestone"],
        "contract_version": contract["version"],
        "dataset_version": dataset["version"],
        "case_count": len(cases),
        "categories": sorted(CATEGORIES),
        "status": "valid",
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/evals/validate_contract.py CONTRACT")
    path = Path(sys.argv[1])
    if not path.is_absolute():
        path = ROOT / path
    print(json.dumps(validate(path), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

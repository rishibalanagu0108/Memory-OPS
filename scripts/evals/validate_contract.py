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
M2_CATEGORIES = {
    "temporal",
    "correction",
    "conflict",
    "expiration",
    "deletion",
    "restore",
    "resurrection",
}
M2_OPERATIONS = {"remember", "inspect", "correct", "retrieve", "expire", "forget", "purge", "restore", "replay"}
M2_RATE_THRESHOLDS = {
    "development_case_pass_rate",
    "temporal_query_exact_rate",
    "admission_decision_exact_rate",
    "immediate_revocation_rate",
    "deletion_completeness_rate",
    "restore_filter_rate",
}
M2_ZERO_THRESHOLDS = {
    "incorrect_current_version_count",
    "silent_conflict_resolution_count",
    "expired_retrieval_count",
    "post_revocation_disclosure_count",
    "incomplete_purge_count",
    "unencrypted_backup_count",
    "untested_restore_count",
    "deleted_content_resurrection_count",
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


def validate_dataset(path: Path, split: str) -> tuple[dict, list]:
    dataset = load(path)
    require(dataset.get("split") == split, f"dataset must use {split} split")
    require(bool(SEMVER.fullmatch(str(dataset.get("version", "")))), "invalid dataset version")
    declared_types = dataset.get("semantic_types")
    require(
        isinstance(declared_types, list)
        and len(declared_types) == len(SEMANTIC_TYPES)
        and set(declared_types) == SEMANTIC_TYPES,
        "semantic types must be complete and unique",
    )

    cases = dataset.get("cases")
    require(isinstance(cases, list) and cases, "dataset must contain cases")
    ids = [case.get("id") for case in cases]
    require(all(ids) and len(ids) == len(set(ids)), "case IDs must be present and unique")
    require({case.get("category") for case in cases} == CATEGORIES, "dataset categories are incomplete")
    require(all(case.get("operation") in {"remember", "inspect", "list"} for case in cases), "unsupported operation")
    require(all(isinstance(case.get("input"), dict) and isinstance(case.get("expected"), dict) for case in cases), "every case needs input and expected objects")

    covered_types = {
        case["input"].get("semantic_type")
        for case in cases
        if case.get("category") == "semantics"
    }
    require(covered_types == SEMANTIC_TYPES, "semantic dataset cases are incomplete")
    return dataset, cases


def validate_m2_dataset(path: Path, split: str = "development") -> tuple[dict, list]:
    dataset = load(path)
    require(dataset.get("split") == split, f"M2 dataset must use {split} split")
    require(bool(SEMVER.fullmatch(str(dataset.get("version", "")))), "invalid dataset version")
    cases = dataset.get("cases")
    require(isinstance(cases, list) and cases, "dataset must contain cases")
    ids = [case.get("id") for case in cases]
    require(all(ids) and len(ids) == len(set(ids)), "case IDs must be present and unique")
    require({case.get("category") for case in cases} == M2_CATEGORIES, "M2 dataset categories are incomplete")
    require(all(case.get("operation") in M2_OPERATIONS for case in cases), "unsupported M2 operation")
    require(
        all(isinstance(case.get("input"), dict) and isinstance(case.get("expected"), dict) for case in cases),
        "every case needs input and expected objects",
    )
    return dataset, cases


def validate_m2(contract: dict) -> dict:
    require(bool(SEMVER.fullmatch(str(contract.get("version", "")))), "invalid contract version")
    status = contract.get("status")
    require(status in {"preimplementation", "verified"}, "invalid M2 contract status")
    require(
        set(contract.get("requirements", ())) == {"FR-8", "FR-11", "NFR-6", "NFR-10", "AC-15"},
        "incorrect M2 requirements",
    )
    holdout = contract.get("holdout")
    if status == "preimplementation":
        require(
            holdout == {"status": "pending", "release_task": "M2-06"},
            "M2 holdout must remain pending before M2-06",
        )
    else:
        require(
            isinstance(holdout, dict)
            and holdout.get("status") == "passed"
            and holdout.get("release_task") == "M2-06"
            and isinstance(holdout.get("dataset"), str)
            and isinstance(holdout.get("result"), str),
            "verified M2 contract requires holdout dataset and result evidence",
        )

    thresholds = contract.get("thresholds", {})
    require(set(thresholds) == M2_RATE_THRESHOLDS | M2_ZERO_THRESHOLDS, "incorrect M2 threshold set")
    require(all(thresholds[name] == 1.0 for name in M2_RATE_THRESHOLDS), "M2 rate thresholds must be 1.0")
    require(all(thresholds[name] == 0 for name in M2_ZERO_THRESHOLDS), "M2 safety thresholds must be zero")

    baseline = contract.get("baseline")
    expected_baseline_status = "not_run" if status == "preimplementation" else "measured"
    require(
        isinstance(baseline, dict)
        and baseline.get("status") == expected_baseline_status
        and baseline.get("required_before_release") is True,
        "M2 baseline state does not match contract status",
    )
    dataset, cases = validate_m2_dataset(ROOT / str(contract.get("dataset", "")))
    if status == "verified":
        holdout_dataset, _ = validate_m2_dataset(
            ROOT / str(holdout["dataset"]), "holdout"
        )
        require(
            (ROOT / str(holdout["result"])).is_file(),
            "verified M2 contract requires published result evidence",
        )
    return {
        "milestone": "m2",
        "contract_version": contract["version"],
        "dataset_version": dataset["version"],
        "holdout_dataset_version": (
            holdout_dataset["version"] if status == "verified" else None
        ),
        "case_count": len(cases),
        "categories": sorted(M2_CATEGORIES),
        "contract_status": contract["status"],
        "status": "valid",
    }


def validate(contract_path: Path) -> dict:
    contract = load(contract_path)
    if contract.get("milestone") == "m2":
        return validate_m2(contract)
    require(contract.get("milestone") == "m1", "contract milestone must be m1")
    require(bool(SEMVER.fullmatch(str(contract.get("version", "")))), "invalid contract version")
    status = contract.get("status")
    require(status in {"preimplementation", "verified"}, "contract status must be preimplementation or verified")
    require(set(contract.get("requirements", ())) == {"FR-4", "FR-5", "AC-15"}, "incorrect requirements")

    thresholds = contract.get("thresholds", {})
    require(set(thresholds) == RATE_THRESHOLDS | ZERO_THRESHOLDS, "incorrect threshold set")
    require(all(thresholds[name] == 1.0 for name in RATE_THRESHOLDS), "rate thresholds must be 1.0")
    require(all(thresholds[name] == 0 for name in ZERO_THRESHOLDS), "safety thresholds must be zero")

    dataset, cases = validate_dataset(ROOT / str(contract.get("dataset", "")), "development")

    holdout = contract.get("holdout")
    require(isinstance(holdout, dict) and holdout.get("release_task") == "M1-07", "holdout release task must be M1-07")
    if status == "preimplementation":
        require(holdout == {"status": "pending", "release_task": "M1-07"}, "preimplementation holdout must be pending")
    else:
        require(holdout.get("status") == "passed", "verified holdout must be passed")
        holdout_dataset, holdout_cases = validate_dataset(ROOT / str(holdout.get("dataset", "")), "holdout")
        result = load(ROOT / str(holdout.get("result", "")))
        require(result.get("status") == "pass", "verified holdout result must pass")
        require(result.get("contract_version") == contract["version"], "holdout contract version mismatch")
        require(result.get("development_dataset_version") == dataset["version"], "development dataset version mismatch")
        require(result.get("holdout_dataset_version") == holdout_dataset["version"], "holdout dataset version mismatch")
        require(result.get("holdout_case_count") == len(holdout_cases), "holdout case count mismatch")
        metrics = result.get("metrics", {})
        require(set(metrics) == set(thresholds), "holdout result metric set mismatch")
        require(all(metrics.get(name) >= thresholds[name] for name in RATE_THRESHOLDS), "holdout rate threshold failed")
        require(all(metrics.get(name) <= thresholds[name] for name in ZERO_THRESHOLDS), "holdout safety threshold failed")

    return {
        "milestone": contract["milestone"],
        "contract_version": contract["version"],
        "dataset_version": dataset["version"],
        "case_count": len(cases),
        "categories": sorted(CATEGORIES),
        "contract_status": status,
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

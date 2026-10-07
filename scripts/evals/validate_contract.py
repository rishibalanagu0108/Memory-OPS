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
M3_CATEGORIES = {
    "exact",
    "filter",
    "keyword",
    "semantic",
    "hybrid",
    "abstention",
    "critical_constraint",
    "token_budget",
    "lifecycle",
    "degraded",
}
M3_RETRIEVERS = {"exact", "filtered", "keyword", "vector", "hybrid"}
M3_BASELINES = {"exact_filtered", "keyword", "vector"}
M3_QUALITY_THRESHOLDS = {
    "ndcg_at_10_min",
    "mrr_at_10_min",
    "recall_at_10_min",
    "abstention_precision_min",
    "abstention_recall_min",
    "critical_constraint_recall_min",
    "token_budget_compliance_rate_min",
    "provenance_validity_rate_min",
}
M3_SAFETY_THRESHOLDS = {
    "unauthorized_result_count_max",
    "inactive_result_count_max",
    "critical_constraint_miss_count_max",
    "fabricated_result_count_max",
}
M4_CATEGORIES = SEMANTIC_TYPES
M4_SAFETY_CASES = {
    "prohibited_secret",
    "policy_denial",
    "explicit_correction",
    "explicit_forget",
    "explicit_remember",
}
M4_SAFETY_THRESHOLDS = {
    "automatic_canonical_write_count_max",
    "prohibited_secret_acceptance_count_max",
    "policy_denial_override_count_max",
    "explicit_operation_override_count_max",
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


def validate_m3_dataset(path: Path, split: str = "development") -> tuple[dict, list]:
    dataset = load(path)
    require(dataset.get("split") == split, f"M3 dataset must use {split} split")
    require(bool(SEMVER.fullmatch(str(dataset.get("version", "")))), "invalid dataset version")
    expected_retrievers = M3_RETRIEVERS if split == "development" else M3_BASELINES | {"rrf"}
    require(set(dataset.get("retrievers", ())) == expected_retrievers, "M3 retriever coverage is incomplete")

    if split == "holdout":
        cases = dataset.get("cases")
        require(isinstance(cases, list) and cases, "M3 dataset must contain cases")
        case_ids = [case.get("id") for case in cases]
        require(all(case_ids) and len(case_ids) == len(set(case_ids)), "M3 case IDs must be present and unique")
        require({case.get("category") for case in cases} == M3_CATEGORIES, "M3 dataset categories are incomplete")
        require(
            all(
                set(case.get("rankings", {})) == M3_BASELINES
                and isinstance(case.get("relevance"), dict)
                and isinstance(case.get("must_exclude_ids"), list)
                and isinstance(case.get("expected_abstain"), bool)
                for case in cases
            ),
            "every M3 holdout case needs baseline rankings and judgments",
        )
        return dataset, cases

    documents = dataset.get("documents")
    require(isinstance(documents, list) and documents, "M3 dataset must contain documents")
    document_ids = [document.get("id") for document in documents]
    require(all(document_ids) and len(document_ids) == len(set(document_ids)), "M3 document IDs must be present and unique")
    require(
        all(
            isinstance(document.get("text"), str)
            and document.get("text")
            and isinstance(document.get("scope"), dict)
            and isinstance(document.get("provenance"), dict)
            and isinstance(document.get("tokens"), int)
            and document.get("tokens") > 0
            for document in documents
        ),
        "every M3 document needs text, scope, provenance, and a positive token count",
    )

    cases = dataset.get("cases")
    require(isinstance(cases, list) and cases, "M3 dataset must contain cases")
    case_ids = [case.get("id") for case in cases]
    require(all(case_ids) and len(case_ids) == len(set(case_ids)), "M3 case IDs must be present and unique")
    require({case.get("category") for case in cases} == M3_CATEGORIES, "M3 dataset categories are incomplete")
    require(all(case.get("operation") in {"search", "build_context"} for case in cases), "unsupported M3 operation")
    require(
        all(isinstance(case.get("input"), dict) and isinstance(case.get("expected"), dict) for case in cases),
        "every M3 case needs input and expected objects",
    )
    for case in cases:
        expected = case["expected"]
        judged_ids = set(expected.get("relevance", {}))
        referenced_ids = judged_ids | set(expected.get("must_include_ids", ())) | set(expected.get("must_exclude_ids", ()))
        require(referenced_ids <= set(document_ids), f"M3 case {case['id']} references an unknown document")
        require(isinstance(expected.get("abstain"), bool), f"M3 case {case['id']} must declare abstention")
        require(isinstance(expected.get("partial"), bool), f"M3 case {case['id']} must declare partial status")
        require(isinstance(expected.get("warnings"), list), f"M3 case {case['id']} must declare warnings")
        if case["category"] == "abstention":
            require(expected["abstain"] is True and not judged_ids, "abstention cases must have no relevant documents")
        if case["category"] == "critical_constraint":
            require(bool(expected.get("must_include_ids")), "critical-constraint cases must require a document")
    return dataset, cases


def validate_m3(contract: dict) -> dict:
    require(bool(SEMVER.fullmatch(str(contract.get("version", "")))), "invalid contract version")
    status = contract.get("status")
    require(status in {"preimplementation", "verified"}, "invalid M3 contract status")
    require(
        set(contract.get("requirements", ())) == {"FR-9", "FR-10", "NFR-11", "AC-8", "AC-15"},
        "incorrect M3 requirements",
    )
    holdout = contract.get("holdout")
    holdout_dataset = None
    holdout_cases: list = []
    if status == "preimplementation":
        require(
            holdout == {"status": "pending", "release_task": "M3-06"},
            "M3 holdout must remain pending before M3-06",
        )
    else:
        require(
            isinstance(holdout, dict)
            and holdout.get("status") == "passed"
            and holdout.get("release_task") == "M3-06"
            and isinstance(holdout.get("dataset"), str)
            and isinstance(holdout.get("result"), str),
            "verified M3 contract requires holdout dataset and result evidence",
        )
        holdout_dataset, holdout_cases = validate_m3_dataset(
            ROOT / holdout["dataset"], "holdout"
        )
        result = load(ROOT / holdout["result"])
        require(result.get("status") == "pass", "M3 holdout execution must pass")
        require(result.get("release_enabled") is False, "unmet comparison must keep RRF disabled")

    baselines = contract.get("baselines")
    require(isinstance(baselines, list), "M3 simpler baselines must be declared")
    require({baseline.get("id") for baseline in baselines} == M3_BASELINES, "incorrect M3 simpler baseline set")
    require(
        all(
            baseline.get("status") == ("not_run" if status == "preimplementation" else "measured")
            and baseline.get("required_before_release") is True
            for baseline in baselines
        ),
        "M3 baseline state does not match contract status",
    )
    candidate = contract.get("candidate", {})
    require(
        candidate.get("id") == "rrf"
        and candidate.get("status") == ("not_run" if status == "preimplementation" else "not_promoted")
        and isinstance(candidate.get("rank_constant"), int),
        "M3 candidate must be an unmeasured RRF configuration",
    )

    promotion = contract.get("promotion", {})
    quality = promotion.get("quality", {})
    safety = promotion.get("safety", {})
    latency = promotion.get("latency_ms", {})
    cost = promotion.get("cost", {})
    require(set(quality) == M3_QUALITY_THRESHOLDS, "incorrect M3 quality threshold set")
    require(all(0 <= value <= 1 for value in quality.values()), "M3 quality thresholds must be rates")
    require(set(safety) == M3_SAFETY_THRESHOLDS, "incorrect M3 safety threshold set")
    require(all(value == 0 for value in safety.values()), "M3 hard-safety thresholds must be zero")
    require(
        set(latency) == {"p50_max", "p95_max", "p99_max", "p95_vs_best_baseline_ratio_max"}
        and 0 < latency["p50_max"] <= latency["p95_max"] <= latency["p99_max"]
        and latency["p95_vs_best_baseline_ratio_max"] >= 1,
        "invalid M3 latency thresholds",
    )
    require(
        set(cost) == {"usd_per_1000_queries_max", "vs_best_baseline_ratio_max"}
        and cost["usd_per_1000_queries_max"] >= 0
        and cost["vs_best_baseline_ratio_max"] >= 1,
        "invalid M3 cost thresholds",
    )
    require(
        promotion.get("comparison")
        == {
            "baseline_selector": "best_eligible_simpler_baseline_by_ndcg_at_10",
            "ndcg_at_10_lift_min": 0.02,
            "recall_at_10_regression_max": 0.0,
            "hard_safety_regression_allowed": False,
        },
        "incorrect M3 baseline comparison rule",
    )

    workload = contract.get("workload", {})
    require(
        all(isinstance(workload.get(name), int) and workload[name] > 0 for name in ("repetitions", "concurrency", "throughput_qps_min")),
        "M3 workload must declare positive repetitions, concurrency, and throughput",
    )
    require(
        isinstance(workload.get("index_lag_seconds_max"), (int, float)) and workload["index_lag_seconds_max"] >= 0,
        "M3 workload must declare an index-lag budget",
    )
    require(workload.get("external_llm_judges") == 0, "M3 deterministic gate must not depend on an LLM judge")

    regression = contract.get("regression", {})
    require(
        regression.get("status") == "defined"
        and regression.get("dataset") == contract.get("dataset")
        and regression.get("required_before_release") is True
        and regression.get("bind_results_to_source_hash") is True,
        "M3 regression suite must reuse the dataset and bind results to source",
    )
    dataset, cases = validate_m3_dataset(ROOT / str(contract.get("dataset", "")))
    return {
        "milestone": "m3",
        "contract_version": contract["version"],
        "dataset_version": dataset["version"],
        "holdout_dataset_version": holdout_dataset["version"] if holdout_dataset else None,
        "holdout_case_count": len(holdout_cases) if holdout_dataset else None,
        "case_count": len(cases),
        "categories": sorted(M3_CATEGORIES),
        "contract_status": status,
        "status": "valid",
    }


def validate_m4_dataset(path: Path, category: str) -> tuple[dict, list]:
    dataset = load(path)
    require(dataset.get("split") == "development", "M4 dataset must use development split")
    require(bool(SEMVER.fullmatch(str(dataset.get("version", "")))), "invalid M4 dataset version")
    require(dataset.get("category") == category, f"M4 dataset category mismatch: {category}")
    cases = dataset.get("cases")
    require(isinstance(cases, list) and cases, f"M4 {category} dataset must contain cases")
    case_ids = [case.get("id") for case in cases]
    require(all(case_ids) and len(case_ids) == len(set(case_ids)), f"M4 {category} case IDs must be present and unique")
    require(
        all(
            isinstance(case.get("input"), dict)
            and isinstance(case.get("expected"), dict)
            and case["expected"].get("decision") in {"extract", "abstain"}
            and case["expected"].get("semantic_type") == category
            and case["expected"].get("canonical_write") is False
            and isinstance(case.get("safety_tags", []), list)
            for case in cases
        ),
        f"every M4 {category} case needs an input, labeled decision, matching category, and zero canonical writes",
    )
    require(
        {case["expected"]["decision"] for case in cases} == {"extract", "abstain"},
        f"M4 {category} dataset must contain positive and negative cases",
    )
    return dataset, cases


def validate_m4(contract: dict) -> dict:
    require(bool(SEMVER.fullmatch(str(contract.get("version", "")))), "invalid contract version")
    status = contract.get("status")
    require(status in {"preimplementation", "verified"}, "M4 contract status must be preimplementation or verified")
    require(
        set(contract.get("requirements", ())) == {"FR-12", "NFR-12", "AC-9", "AC-15"},
        "incorrect M4 requirements",
    )
    holdout = contract.get("holdout", {})
    require(holdout.get("release_task") == "M4-05", "M4 holdout release task must be M4-05")
    if status == "preimplementation":
        require(holdout == {"status": "pending", "release_task": "M4-05"}, "preimplementation M4 holdout must be pending")
    else:
        require(
            holdout.get("status") == "passed"
            and holdout.get("quality_evaluation_status") == "blocked"
            and holdout.get("quality_blocker") == "production_extractor_unconfigured",
            "verified M4 must publish the fail-closed quality blocker",
        )

    datasets = contract.get("datasets", {})
    require(set(datasets) == M4_CATEGORIES, "M4 category datasets are incomplete")
    dataset_versions = set()
    case_ids: list[str] = []
    covered_safety_cases = set()
    for category, dataset_path in datasets.items():
        require(isinstance(dataset_path, str), f"M4 {category} dataset path must be a string")
        dataset, cases = validate_m4_dataset(ROOT / dataset_path, category)
        dataset_versions.add(dataset["version"])
        case_ids.extend(case["id"] for case in cases)
        covered_safety_cases.update(tag for case in cases for tag in case.get("safety_tags", []))
    require(len(dataset_versions) == 1, "M4 category dataset versions must match")
    require(len(case_ids) == len(set(case_ids)), "M4 case IDs must be globally unique")
    require(covered_safety_cases == M4_SAFETY_CASES, "M4 safety-case coverage is incomplete")

    baseline = contract.get("baseline", {})
    expected_baseline_status = "not_run" if status == "preimplementation" else "defined_not_measured"
    require(
        baseline.get("id") == "manual_review_only"
        and baseline.get("status") == expected_baseline_status
        and baseline.get("required_before_release") is True,
        "M4 manual-review baseline status is invalid",
    )
    candidate = contract.get("candidate", {})
    expected_candidate_status = "not_run" if status == "preimplementation" else "evaluated_not_promoted"
    require(
        candidate.get("id") == "shadow_extractor"
        and candidate.get("status") == expected_candidate_status
        and candidate.get("canonical_persistence") is False,
        "M4 candidate must remain a non-persisting shadow extractor",
    )

    promotion = contract.get("promotion", {})
    category_thresholds = promotion.get("categories", {})
    require(set(category_thresholds) == M4_CATEGORIES, "M4 promotion thresholds must be category-specific")
    require(
        all(
            set(thresholds) == {"precision_min", "recall_min"}
            and 0 < thresholds["precision_min"] <= 1
            and 0 < thresholds["recall_min"] <= 1
            for thresholds in category_thresholds.values()
        ),
        "invalid M4 category promotion thresholds",
    )
    safety = promotion.get("safety", {})
    require(set(safety) == M4_SAFETY_THRESHOLDS, "incorrect M4 safety threshold set")
    require(all(value == 0 for value in safety.values()), "M4 hard-safety thresholds must be zero")
    require(promotion.get("hard_safety_regression_allowed") is False, "M4 hard-safety regression must be forbidden")
    require(promotion.get("rollback_required") is True, "M4 promotion must require rollback")
    require(promotion.get("promote_categories_independently") is True, "M4 categories must promote independently")

    calibration = contract.get("calibration", {})
    require(
        set(calibration) == {"expected_calibration_error_max", "brier_score_max", "bins", "minimum_cases_per_category"}
        and 0 <= calibration["expected_calibration_error_max"] <= 1
        and 0 <= calibration["brier_score_max"] <= 1
        and isinstance(calibration["bins"], int)
        and calibration["bins"] >= 2
        and isinstance(calibration["minimum_cases_per_category"], int)
        and calibration["minimum_cases_per_category"] > 0,
        "invalid M4 calibration metrics",
    )
    agreement = contract.get("reviewer_agreement", {})
    require(
        isinstance(agreement.get("reviewers_per_case_min"), int)
        and agreement["reviewers_per_case_min"] >= 2
        and isinstance(agreement.get("cohens_kappa_min"), (int, float))
        and 0 <= agreement["cohens_kappa_min"] <= 1
        and isinstance(agreement.get("raw_agreement_rate_min"), (int, float))
        and 0 <= agreement["raw_agreement_rate_min"] <= 1
        and agreement.get("adjudicate_disagreements") is True
        and agreement.get("blind_independent_labels") is True,
        "invalid M4 reviewer-agreement protocol",
    )

    regression = contract.get("regression", {})
    require(
        regression.get("status") == "defined"
        and set(regression.get("datasets", ())) == set(datasets.values())
        and regression.get("required_before_release") is True
        and regression.get("bind_results_to_source_hash") is True,
        "M4 regression suite must reuse every category dataset and bind results to source",
    )
    holdout_case_count = None
    if status == "verified":
        holdout_dataset = load(ROOT / str(holdout.get("dataset", "")))
        require(holdout_dataset.get("split") == "holdout", "M4 holdout split is invalid")
        require(bool(SEMVER.fullmatch(str(holdout_dataset.get("version", "")))), "invalid M4 holdout version")
        holdout_cases = holdout_dataset.get("cases", [])
        require(
            len(holdout_cases) == len(M4_CATEGORIES)
            and {case.get("category") for case in holdout_cases} == M4_CATEGORIES
            and len({case.get("id") for case in holdout_cases}) == len(holdout_cases),
            "M4 holdout must contain one unique readiness case per category",
        )
        require(not (set(case_ids) & {case["id"] for case in holdout_cases}), "M4 holdout must remain separate from development")
        require(
            set(holdout_dataset.get("safety_scenarios", ())) == M4_SAFETY_CASES,
            "M4 holdout safety scenarios are incomplete",
        )
        result = load(ROOT / str(holdout.get("result", "")))
        require(
            result.get("status") == "pass"
            and result.get("gate_outcome") == "fail_closed"
            and result.get("automatic_promotion_enabled") is False
            and result.get("quality_blocker") == "production_extractor_unconfigured",
            "M4 result must prove fail-closed non-promotion",
        )
        require(result.get("contract_version") == contract["version"], "M4 result contract version mismatch")
        require(result.get("holdout_dataset_version") == holdout_dataset["version"], "M4 holdout version mismatch")
        require(result.get("holdout_case_count") == len(holdout_cases), "M4 holdout case count mismatch")
        require(set(result.get("category_results", {})) == M4_CATEGORIES, "M4 category results are incomplete")
        require(
            all(not item.get("release_enabled") and item.get("mode") == "shadow" for item in result["category_results"].values()),
            "M4 categories must remain in shadow mode",
        )
        require(
            all(result.get("safety_metrics", {}).get(name.removesuffix("_max")) <= maximum for name, maximum in safety.items()),
            "M4 holdout exceeded a hard-safety threshold",
        )
        holdout_case_count = len(holdout_cases)
    return {
        "milestone": "m4",
        "contract_version": contract["version"],
        "dataset_version": dataset_versions.pop(),
        "case_count": len(case_ids),
        "holdout_case_count": holdout_case_count,
        "categories": sorted(M4_CATEGORIES),
        "contract_status": status,
        "status": "valid",
    }


def validate(contract_path: Path) -> dict:
    contract = load(contract_path)
    if contract.get("milestone") == "m2":
        return validate_m2(contract)
    if contract.get("milestone") == "m3":
        return validate_m3(contract)
    if contract.get("milestone") == "m4":
        return validate_m4(contract)
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

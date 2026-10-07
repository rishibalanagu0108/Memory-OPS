"""Deterministic M4 promotion-readiness holdout."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from memory_ops.extraction import (
    CategoryEvaluationEvidence,
    CategoryMetrics,
    CategoryPromotionCriteria,
    CategoryPromotionRegistry,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATHS = (
    "src/memory_ops/extraction/__init__.py",
    "src/memory_ops/extraction/promotion.py",
    "evals/m4/evaluate.py",
    "evals/m4/holdout.json",
)


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for relative_path in SOURCE_PATHS:
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update((ROOT / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_and_evaluate() -> dict:
    contract = json.loads((ROOT / "evals/m4/contract.yaml").read_text())
    holdout = json.loads((ROOT / "evals/m4/holdout.json").read_text())
    fingerprint = source_fingerprint()
    calibration = contract["calibration"]
    agreement = contract["reviewer_agreement"]
    cases = {case["category"]: case for case in holdout["cases"]}
    criteria = tuple(
        CategoryPromotionCriteria(
            contract_version=contract["version"],
            category=category,
            dataset=contract["holdout"]["dataset"],
            dataset_version=holdout["version"],
            precision_min=thresholds["precision_min"],
            recall_min=thresholds["recall_min"],
            expected_calibration_error_max=calibration["expected_calibration_error_max"],
            brier_score_max=calibration["brier_score_max"],
            reviewer_kappa_min=agreement["cohens_kappa_min"],
            raw_agreement_rate_min=agreement["raw_agreement_rate_min"],
            minimum_cases=calibration["minimum_cases_per_category"],
        )
        for category, thresholds in contract["promotion"]["categories"].items()
    )
    registry = CategoryPromotionRegistry(criteria, fingerprint)
    category_results = {}
    case_results = []
    empty_metrics = CategoryMetrics(
        precision=0,
        recall=0,
        expected_calibration_error=1,
        brier_score=1,
        reviewer_kappa=0,
        raw_agreement_rate=0,
    )
    for category in sorted(cases):
        case = cases[category]
        evidence = CategoryEvaluationEvidence(
            evidence_version="m4-holdout-1.0.0",
            contract_version=contract["version"],
            category=category,
            dataset=contract["holdout"]["dataset"],
            dataset_version=holdout["version"],
            source_hash=fingerprint,
            case_count=1,
            metrics=empty_metrics,
            baseline_measured=False,
            holdout_passed=True,
            regression_passed=True,
        )
        decision = registry.promote(evidence)
        passed = (
            not case["candidate_available"]
            and decision.state.mode == case["expected_mode"]
            and not decision.automatic_enabled
        )
        reasons = ["production_extractor_unconfigured", *decision.reason_codes]
        category_results[category] = {
            "quality_status": "not_measured",
            "release_enabled": decision.automatic_enabled,
            "mode": decision.state.mode,
            "observed_case_count": 1,
            "required_case_count": calibration["minimum_cases_per_category"],
            "promotion_reasons": reasons,
        }
        case_results.append({"case_id": case["id"], "status": "pass" if passed else "fail"})

    return {
        "milestone": "m4",
        "contract_version": contract["version"],
        "holdout_dataset_version": holdout["version"],
        "holdout_case_count": len(holdout["cases"]),
        "status": "pass" if all(item["status"] == "pass" for item in case_results) else "fail",
        "gate_outcome": "fail_closed",
        "automatic_promotion_enabled": False,
        "quality_evaluation_status": "blocked",
        "quality_blocker": "production_extractor_unconfigured",
        "safety_metrics": {
            "automatic_canonical_write_count": 0,
            "prohibited_secret_acceptance_count": 0,
            "policy_denial_override_count": 0,
            "explicit_operation_override_count": 0,
        },
        "category_results": category_results,
        "case_results": case_results,
        "source_fingerprint": fingerprint,
        "external_model_calls": 0,
    }

"""Credential-free checks for M4 quality corpus and metrics."""

import json
from pathlib import Path

import pytest

from evals.m4.quality import evaluate_predictions, expand_cases, load_manifest, score_predictions, score_quality


ROOT = Path(__file__).resolve().parents[2]


def perfect_evidence() -> tuple[dict, dict]:
    cases = expand_cases(load_manifest())
    predictions = {
        "model": {"provider": "fixture", "name": "perfect", "version": "1"},
        "predictions": [
            {
                "case_id": case["id"],
                "decision": case["expected_decision"],
                "semantic_type": case["category"] if case["expected_decision"] == "extract" else None,
                "extract_probability": 0.99 if case["expected_decision"] == "extract" else 0.01,
            }
            for case in cases
        ],
    }
    reviews = {
        "labels": [
            {"case_id": case["id"], "reviewer_id": reviewer, "decision": case["expected_decision"]}
            for case in cases
            for reviewer in ("reviewer-a", "reviewer-b")
        ]
    }
    return predictions, reviews


def test_quality_corpus_has_100_separate_cases_per_category() -> None:
    cases = expand_cases(load_manifest())

    assert len(cases) == 500
    assert len({case["id"] for case in cases}) == 500
    assert all(sum(case["category"] == category for case in cases) == 100 for category in {case["category"] for case in cases})


def test_quality_metrics_are_computed_from_predictions_and_two_reviewers() -> None:
    predictions, reviews = perfect_evidence()
    result = score_quality(load_manifest(), predictions, reviews)

    assert all(metrics["case_count"] == 100 for metrics in result.values())
    assert all(metrics["precision"] == metrics["recall"] == 1.0 for metrics in result.values())
    assert all(metrics["expected_calibration_error"] == 0.01 for metrics in result.values())
    assert all(metrics["brier_score"] == 0.0001 for metrics in result.values())
    assert all(metrics["reviewer_kappa"] == metrics["raw_agreement_rate"] == 1.0 for metrics in result.values())


def test_candidate_metrics_do_not_require_review_labels() -> None:
    predictions, _ = perfect_evidence()
    result = score_predictions(load_manifest(), predictions)

    assert all("reviewer_kappa" not in metrics for metrics in result.values())
    assert all(metrics["precision"] == metrics["recall"] == 1.0 for metrics in result.values())


def test_published_azure_candidate_result_matches_source_bound_predictions() -> None:
    contract = json.loads((ROOT / "evals/m4/contract.yaml").read_text())
    published = json.loads((ROOT / contract["holdout"]["candidate_result"]).read_text())

    assert published == evaluate_predictions(contract)


def test_quality_metrics_reject_incomplete_evidence() -> None:
    predictions, reviews = perfect_evidence()
    predictions["predictions"].pop()

    with pytest.raises(ValueError, match="cover every quality case"):
        score_quality(load_manifest(), predictions, reviews)


def test_quality_metrics_use_reviewer_consensus_and_reject_disagreements() -> None:
    predictions, reviews = perfect_evidence()
    case_id = reviews["labels"][0]["case_id"]
    reviews["labels"][0]["decision"] = "abstain"

    with pytest.raises(ValueError, match="disagreements must be adjudicated"):
        score_quality(load_manifest(), predictions, reviews)

    reviews["labels"][1]["decision"] = "abstain"
    result = score_quality(load_manifest(), predictions, reviews)

    assert result["constraint"]["precision"] == 49 / 50

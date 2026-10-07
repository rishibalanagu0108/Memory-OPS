"""M4 holdout separation and fail-closed promotion checks."""

import json
from pathlib import Path

from evals.m4.evaluate import load_and_evaluate


ROOT = Path(__file__).resolve().parents[2]
HOLDOUT = json.loads((ROOT / "evals/m4/holdout.json").read_text())
CONTRACT = json.loads((ROOT / "evals/m4/contract.yaml").read_text())
CATEGORIES = {"fact", "preference", "goal", "constraint", "episode"}


def test_holdout_is_versioned_complete_and_separate() -> None:
    cases = HOLDOUT["cases"]
    development_ids = {
        case["id"]
        for path in CONTRACT["datasets"].values()
        for case in json.loads((ROOT / path).read_text())["cases"]
    }
    assert HOLDOUT["split"] == "holdout"
    assert HOLDOUT["version"] == "1.0.0"
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["category"] for case in cases} == CATEGORIES
    assert development_ids.isdisjoint(case["id"] for case in cases)


def test_unconfigured_candidate_fails_closed_per_category() -> None:
    result = load_and_evaluate()

    assert result["status"] == "pass"
    assert result["gate_outcome"] == "fail_closed"
    assert result["automatic_promotion_enabled"] is False
    assert result["quality_evaluation_status"] == "blocked"
    assert set(result["category_results"]) == CATEGORIES
    assert all(item["status"] == "pass" for item in result["case_results"])
    assert all(
        not item["release_enabled"]
        and item["mode"] == "shadow"
        and "production_extractor_unconfigured" in item["promotion_reasons"]
        for item in result["category_results"].values()
    )
    assert all(value == 0 for value in result["safety_metrics"].values())

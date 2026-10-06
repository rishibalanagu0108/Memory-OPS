"""Protected M3 holdout invariants and deterministic comparison."""

import json
from pathlib import Path

from evals.m3.evaluate import load_and_evaluate


ROOT = Path(__file__).resolve().parents[2]
HOLDOUT = json.loads((ROOT / "evals/m3/holdout.json").read_text())
GOLDEN = json.loads((ROOT / "evals/m3/golden.json").read_text())


def test_holdout_is_complete_versioned_and_separate() -> None:
    cases = HOLDOUT["cases"]
    assert HOLDOUT["split"] == "holdout"
    assert HOLDOUT["version"] == "1.0.0"
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["id"] for case in cases}.isdisjoint(
        case["id"] for case in GOLDEN["cases"]
    )
    assert {case["category"] for case in cases} == {
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


def test_rrf_holdout_is_safe_and_meets_quality_floors() -> None:
    result = load_and_evaluate()
    rrf = result["systems"]["rrf"]

    assert all(case["status"] == "pass" for case in result["case_results"])
    assert rrf["ndcg_at_10"] >= 0.85
    assert rrf["mrr_at_10"] >= 0.85
    assert rrf["recall_at_10"] >= 0.95
    assert rrf["abstention_accuracy"] == 1.0
    assert rrf["hard_safety_failures"] == 0
    assert result["critical_constraint_recall"] == 1.0
    assert result["token_budget_compliance_rate"] == 1.0
    assert result["provenance_validity_rate"] == 1.0

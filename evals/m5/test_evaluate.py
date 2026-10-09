import json
from pathlib import Path

import pytest

from evals.m5.evaluate import evaluate, evaluate_files


ROOT = Path(__file__).resolve().parents[2]


def load(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text())


def test_published_result_matches_current_paired_evidence() -> None:
    contract = load("evals/m5/contract.yaml")
    published = load("evals/m5/result.json")

    assert evaluate_files(contract) == published
    assert published["status"] == "pass"
    assert published["promotion_eligible"] is True
    assert published["automatic_promotion_enabled"] is False
    assert published["metrics"]["task_quality_delta_lower_confidence_bound"] > 0
    assert published["metrics"]["policy_override_count"] == 0
    assert published["gate_failures"] == []


def test_evaluator_rejects_incomplete_pairing() -> None:
    contract = load("evals/m5/contract.yaml")
    dataset = load("evals/m5/holdout.json")
    evidence = load("evals/m5/observations.json")
    evidence["observations"].pop()

    with pytest.raises(ValueError, match="cover each case"):
        evaluate(contract, dataset, evidence)

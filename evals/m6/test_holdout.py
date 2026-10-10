import json
from pathlib import Path

import pytest

from evals.m6.evaluate import evaluate, evaluate_files


ROOT = Path(__file__).resolve().parents[2]


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def test_holdout_is_protected_complete_and_separate() -> None:
    holdout = load("evals/m6/holdout.json")
    golden = load("evals/m6/golden.json")

    assert holdout["split"] == "protected_holdout"
    assert {case["category"] for case in holdout["cases"]} == set(holdout["categories"])
    assert {case["id"] for case in holdout["cases"]}.isdisjoint(
        case["id"] for case in golden["cases"]
    )


def test_published_result_matches_current_source_bound_evidence() -> None:
    contract = load("evals/m6/contract.yaml")
    published = load("evals/m6/result.json")
    evaluated = evaluate_files(contract)

    deterministic = (
        "parse_structure_fidelity_rate",
        "passage_recall_at_10",
        "current_source_precision",
        "conflict_detection_rate",
        "freshness_detection_rate",
        "citation_exact_rate",
        "unauthorized_passage_count",
        "noncurrent_passage_count",
        "prompt_instruction_execution_count",
        "fabricated_citation_count",
    )
    assert {name: published["metrics"][name] for name in deterministic} == {
        name: evaluated["metrics"][name] for name in deterministic
    }
    assert published["source_fingerprint"] == evaluated["source_fingerprint"]
    assert published["status"] == "pass"
    assert published["promotion_eligible"] is True
    assert published["gate_failures"] == []


def test_evaluator_rejects_incomplete_category_coverage() -> None:
    contract = load("evals/m6/contract.yaml")
    holdout = load("evals/m6/holdout.json")
    holdout["cases"].pop()

    with pytest.raises(ValueError, match="categories are incomplete"):
        evaluate(contract, holdout)

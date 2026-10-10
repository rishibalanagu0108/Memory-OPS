import json
from pathlib import Path

from evals.m7.evaluate import evaluate_files


ROOT = Path(__file__).resolve().parents[2]


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def test_holdout_is_protected_complete_and_separate() -> None:
    holdout = load("evals/m7/holdout.json")
    golden = load("evals/m7/golden.json")

    assert holdout["split"] == "protected_holdout"
    assert {case["category"] for case in holdout["cases"]} == set(
        holdout["categories"]
    )
    assert {case["id"] for case in holdout["cases"]}.isdisjoint(
        case["id"] for case in golden["cases"]
    )


def test_published_m7_holdout_matches_current_evidence() -> None:
    contract = load("evals/m7/contract.yaml")
    published = load(contract["holdout"]["result"])
    evaluated = evaluate_files(contract)

    assert evaluated["status"] == "pass"
    assert evaluated["promotion_eligible"] is True
    assert evaluated["gate_failures"] == []
    assert published["source_fingerprint"] == evaluated["source_fingerprint"]
    deterministic = (
        "domain_label_accuracy_rate",
        "authority_preservation_rate",
        "conflict_detection_rate",
        "missing_domain_reporting_rate",
        "token_budget_compliance_rate",
        "end_task_success_rate",
        "end_task_quality_delta_lower_confidence_bound",
        "unauthorized_item_count",
        "silent_authority_override_count",
        "fabricated_completeness_count",
        "unattributed_item_count",
    )
    assert all(
        published["metrics"][name] == evaluated["metrics"][name]
        for name in deterministic
    )

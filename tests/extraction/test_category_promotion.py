import pytest

from memory_ops.extraction import (
    CategoryEvaluationEvidence,
    CategoryMetrics,
    CategoryPromotionCriteria,
    CategoryPromotionRegistry,
)


def metrics(**changes: object) -> CategoryMetrics:
    values = {
        "precision": 0.99,
        "recall": 0.95,
        "expected_calibration_error": 0.03,
        "brier_score": 0.08,
        "reviewer_kappa": 0.85,
        "raw_agreement_rate": 0.95,
    }
    values.update(changes)
    return CategoryMetrics.model_validate(values)


def criterion(category: str, precision: float = 0.98) -> CategoryPromotionCriteria:
    return CategoryPromotionCriteria.model_validate(
        {
            "contract_version": "1.0.0",
            "category": category,
            "dataset": f"evals/m4/{category}.json",
            "dataset_version": "1.0.0",
            "precision_min": precision,
            "recall_min": 0.90,
            "expected_calibration_error_max": 0.05,
            "brier_score_max": 0.10,
            "reviewer_kappa_min": 0.80,
            "raw_agreement_rate_min": 0.90,
            "minimum_cases": 100,
        }
    )


def evidence(category: str = "preference", **changes: object) -> CategoryEvaluationEvidence:
    values = {
        "evidence_version": "evidence-1",
        "contract_version": "1.0.0",
        "category": category,
        "dataset": f"evals/m4/{category}.json",
        "dataset_version": "1.0.0",
        "source_hash": "source-abc123",
        "case_count": 100,
        "metrics": metrics(),
        "baseline_measured": True,
        "holdout_passed": True,
        "regression_passed": True,
        "approved_by": "reviewer-1",
        "approval_reference": "approval-1",
    }
    values.update(changes)
    return CategoryEvaluationEvidence.model_validate(values)


def registry() -> CategoryPromotionRegistry:
    return CategoryPromotionRegistry(
        (criterion("preference"), criterion("constraint", precision=0.995)),
        current_source_hash="source-abc123",
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"approved_by": None}, "approval"),
        ({"holdout_passed": False}, "protected_holdout"),
        ({"baseline_measured": False}, "baseline"),
        ({"regression_passed": False}, "regression"),
        ({"case_count": 99}, "minimum_cases"),
        ({"source_hash": "stale-source"}, "source_hash"),
        ({"contract_version": "0.9.0"}, "contract_version"),
        ({"metrics": metrics(precision=0.50)}, "precision"),
        (
            {"metrics": metrics(prohibited_secret_acceptance_count=1)},
            "prohibited_secret",
        ),
    ],
)
def test_promotion_fails_closed_without_complete_approved_evidence(
    change: dict[str, object], reason: str
) -> None:
    controls = registry()

    decision = controls.promote(evidence(**change))

    assert decision.changed is False
    assert decision.automatic_enabled is False
    assert decision.state.mode == "shadow"
    assert reason in decision.reason_codes


def test_categories_promote_independently_with_versioned_evidence() -> None:
    controls = registry()

    promoted = controls.promote(evidence())

    assert promoted.automatic_enabled is True
    assert promoted.state.mode == "automatic"
    assert promoted.state.version == 2
    assert promoted.state.evidence_version == "evidence-1"
    assert controls.current("constraint").mode == "shadow"


def test_monitoring_safety_regression_pauses_only_the_promoted_category() -> None:
    controls = registry()
    controls.promote(evidence())

    paused = controls.monitor(
        "preference", metrics(policy_denial_override_count=1)
    )

    assert paused.changed is True
    assert paused.automatic_enabled is False
    assert paused.state.mode == "paused"
    assert paused.state.version == 3
    assert paused.reason_codes == ("monitor_policy_denial_override",)
    assert controls.current("constraint").mode == "shadow"


def test_pause_and_rollback_append_auditable_versions() -> None:
    controls = registry()
    controls.promote(evidence())

    paused = controls.pause("preference", "operator_pause")
    rolled_back = controls.rollback("preference", "rollback_requested")

    assert paused.state.version == 3
    assert rolled_back.state.version == 4
    assert rolled_back.state.mode == "shadow"
    assert rolled_back.state.previous_version == 3
    assert rolled_back.state.evidence_version is None
    assert [state.version for state in controls.history("preference")] == [1, 2, 3, 4]

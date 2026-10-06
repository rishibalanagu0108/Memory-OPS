"""Evidence-gated, per-category extraction promotion controls."""

from typing import Annotated, Literal

from pydantic import Field

from memory_ops.user_memory import BoundedText, DomainModel, SemanticType


Rate = Annotated[float, Field(ge=0, le=1)]
Count = Annotated[int, Field(ge=0)]


class CategoryMetrics(DomainModel):
    precision: Rate
    recall: Rate
    expected_calibration_error: Rate
    brier_score: Rate
    reviewer_kappa: Rate
    raw_agreement_rate: Rate
    automatic_canonical_write_count: Count = 0
    prohibited_secret_acceptance_count: Count = 0
    policy_denial_override_count: Count = 0
    explicit_operation_override_count: Count = 0


class CategoryPromotionCriteria(DomainModel):
    contract_version: BoundedText
    category: SemanticType
    dataset: BoundedText
    dataset_version: BoundedText
    precision_min: Rate
    recall_min: Rate
    expected_calibration_error_max: Rate
    brier_score_max: Rate
    reviewer_kappa_min: Rate
    raw_agreement_rate_min: Rate
    minimum_cases: Annotated[int, Field(gt=0)]


class CategoryEvaluationEvidence(DomainModel):
    evidence_version: BoundedText
    contract_version: BoundedText
    category: SemanticType
    dataset: BoundedText
    dataset_version: BoundedText
    source_hash: BoundedText
    case_count: Annotated[int, Field(gt=0)]
    metrics: CategoryMetrics
    baseline_measured: bool
    holdout_passed: bool
    regression_passed: bool
    approved_by: BoundedText | None = None
    approval_reference: BoundedText | None = None


class CategoryPromotionState(DomainModel):
    category: SemanticType
    version: Annotated[int, Field(gt=0)]
    mode: Literal["shadow", "automatic", "paused"]
    evidence_version: BoundedText | None = None
    previous_version: int | None = None
    reason_codes: tuple[BoundedText, ...] = ()


class CategoryControlDecision(DomainModel):
    state: CategoryPromotionState
    changed: bool
    automatic_enabled: bool
    reason_codes: tuple[BoundedText, ...] = ()


class CategoryPromotionRegistry:
    """Version category controls independently and fail closed on stale evidence."""

    def __init__(
        self,
        criteria: tuple[CategoryPromotionCriteria, ...],
        current_source_hash: str,
    ) -> None:
        if not criteria:
            raise ValueError("at least one category criterion is required")
        if not current_source_hash.strip():
            raise ValueError("current source hash is required")
        self._criteria = {item.category: item for item in criteria}
        if len(self._criteria) != len(criteria):
            raise ValueError("category criteria must be unique")
        self._current_source_hash = current_source_hash
        # ponytail: in-process history; use transactional storage before a
        # multi-instance control plane mutates promotion state.
        self._history = {
            category: (
                CategoryPromotionState(
                    category=category,
                    version=1,
                    mode="shadow",
                    reason_codes=("not_promoted",),
                ),
            )
            for category in self._criteria
        }

    def current(self, category: SemanticType) -> CategoryPromotionState:
        return self._category_history(category)[-1]

    def history(self, category: SemanticType) -> tuple[CategoryPromotionState, ...]:
        return self._category_history(category)

    def promote(
        self, evidence: CategoryEvaluationEvidence
    ) -> CategoryControlDecision:
        criteria = self._criterion(evidence.category)
        reasons = self._evidence_failures(evidence, criteria)
        if reasons:
            return self._unchanged(evidence.category, reasons)
        return self._append(
            evidence.category,
            "automatic",
            (),
            evidence_version=evidence.evidence_version,
        )

    def monitor(
        self, category: SemanticType, metrics: CategoryMetrics
    ) -> CategoryControlDecision:
        state = self.current(category)
        if state.mode != "automatic":
            return self._unchanged(category, ("not_automatic",))
        reasons = self._metric_failures(metrics, self._criterion(category))
        if not reasons:
            return CategoryControlDecision(
                state=state,
                changed=False,
                automatic_enabled=True,
            )
        return self._append(
            category,
            "paused",
            tuple(f"monitor_{reason}" for reason in reasons),
            evidence_version=state.evidence_version,
        )

    def pause(self, category: SemanticType, reason: str) -> CategoryControlDecision:
        if not reason.strip():
            raise ValueError("pause reason is required")
        state = self.current(category)
        if state.mode == "paused":
            return self._unchanged(category, ("already_paused",))
        return self._append(
            category,
            "paused",
            (reason,),
            evidence_version=state.evidence_version,
        )

    def rollback(self, category: SemanticType, reason: str) -> CategoryControlDecision:
        if not reason.strip():
            raise ValueError("rollback reason is required")
        return self._append(category, "shadow", (reason,))

    def _append(
        self,
        category: SemanticType,
        mode: Literal["shadow", "automatic", "paused"],
        reasons: tuple[str, ...],
        *,
        evidence_version: str | None = None,
    ) -> CategoryControlDecision:
        previous = self.current(category)
        state = CategoryPromotionState(
            category=category,
            version=previous.version + 1,
            mode=mode,
            evidence_version=evidence_version,
            previous_version=previous.version,
            reason_codes=reasons,
        )
        self._history[category] += (state,)
        return CategoryControlDecision(
            state=state,
            changed=True,
            automatic_enabled=mode == "automatic",
            reason_codes=reasons,
        )

    def _unchanged(
        self, category: SemanticType, reasons: tuple[str, ...]
    ) -> CategoryControlDecision:
        state = self.current(category)
        return CategoryControlDecision(
            state=state,
            changed=False,
            automatic_enabled=state.mode == "automatic",
            reason_codes=reasons,
        )

    def _criterion(self, category: SemanticType) -> CategoryPromotionCriteria:
        try:
            return self._criteria[category]
        except KeyError as error:
            raise ValueError(f"unconfigured category: {category}") from error

    def _category_history(
        self, category: SemanticType
    ) -> tuple[CategoryPromotionState, ...]:
        try:
            return self._history[category]
        except KeyError as error:
            raise ValueError(f"unconfigured category: {category}") from error

    def _evidence_failures(
        self,
        evidence: CategoryEvaluationEvidence,
        criteria: CategoryPromotionCriteria,
    ) -> tuple[str, ...]:
        checks = {
            "contract_version": evidence.contract_version
            == criteria.contract_version,
            "dataset": evidence.dataset == criteria.dataset,
            "dataset_version": evidence.dataset_version == criteria.dataset_version,
            "source_hash": evidence.source_hash == self._current_source_hash,
            "minimum_cases": evidence.case_count >= criteria.minimum_cases,
            "baseline": evidence.baseline_measured,
            "protected_holdout": evidence.holdout_passed,
            "regression": evidence.regression_passed,
            "approval": bool(evidence.approved_by and evidence.approval_reference),
        }
        reasons = tuple(name for name, passed in checks.items() if not passed)
        return reasons + self._metric_failures(evidence.metrics, criteria)

    @staticmethod
    def _metric_failures(
        metrics: CategoryMetrics, criteria: CategoryPromotionCriteria
    ) -> tuple[str, ...]:
        checks = {
            "precision": metrics.precision >= criteria.precision_min,
            "recall": metrics.recall >= criteria.recall_min,
            "calibration": metrics.expected_calibration_error
            <= criteria.expected_calibration_error_max,
            "brier_score": metrics.brier_score <= criteria.brier_score_max,
            "reviewer_kappa": metrics.reviewer_kappa >= criteria.reviewer_kappa_min,
            "reviewer_agreement": metrics.raw_agreement_rate
            >= criteria.raw_agreement_rate_min,
            "automatic_canonical_write": metrics.automatic_canonical_write_count == 0,
            "prohibited_secret": metrics.prohibited_secret_acceptance_count == 0,
            "policy_denial_override": metrics.policy_denial_override_count == 0,
            "explicit_operation_override": metrics.explicit_operation_override_count
            == 0,
        }
        return tuple(name for name, passed in checks.items() if not passed)


__all__ = [
    "CategoryControlDecision",
    "CategoryEvaluationEvidence",
    "CategoryMetrics",
    "CategoryPromotionCriteria",
    "CategoryPromotionRegistry",
    "CategoryPromotionState",
]

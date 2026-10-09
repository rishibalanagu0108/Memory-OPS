"""Evidence-gated canary promotion, monitoring, and rollback for lessons."""

from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import text

from memory_ops.agent_learning import (
    AgentLearningModel,
    AgentLearningStore,
    BoundedText,
    LessonModelIdentity,
    LessonScope,
    LessonVersion,
    ToolIdentity,
)
from memory_ops.persistence import TenantDatabase


Rate = Annotated[float, Field(ge=0, le=1)]
Count = Annotated[int, Field(ge=0)]


class LessonPromotionNotFound(Exception):
    pass


class LessonMonitoringMetrics(AgentLearningModel):
    pair_count: Annotated[int, Field(ge=1)]
    task_quality_delta_lower_confidence_bound: float
    candidate_task_success_rate: Rate
    repeated_failure_rate_regression: float
    applicable_lesson_use_rate: Rate
    non_applicable_lesson_use_count: Count = 0
    scope_mismatch_use_count: Count = 0
    critical_safety_regression_count: Count = 0
    policy_override_count: Count = 0
    credential_or_private_reasoning_persistence_count: Count = 0
    cross_tenant_lesson_use_count: Count = 0
    p95_latency_ms: Annotated[float, Field(ge=0)]
    p95_vs_baseline_ratio: Annotated[float, Field(ge=0)]
    usd_per_1000_tasks: Annotated[float, Field(ge=0)]
    cost_vs_baseline_ratio: Annotated[float, Field(ge=0)]


class LessonPromotionEvidence(AgentLearningModel):
    evidence_id: BoundedText
    candidate_id: UUID
    evaluated_version_id: UUID
    contract_version: BoundedText
    dataset_version: BoundedText
    source_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    baseline_measured: bool
    protected_holdout_passed: bool
    approved_by: BoundedText | None = None
    approval_reference: BoundedText | None = None
    rollback_reference: BoundedText | None = None
    metrics: LessonMonitoringMetrics


class LessonControlState(AgentLearningModel):
    candidate_id: UUID
    scope: LessonScope
    control_version: Annotated[int, Field(ge=1)]
    mode: Literal["shadow", "canary", "active", "paused", "rolled_back"]
    evaluated_version_id: UUID
    promoted_version_id: UUID | None = None
    evidence_id: BoundedText | None = None
    canary_percent: Annotated[int, Field(ge=0, le=100)] = 0
    previous_control_version: int | None = None
    reason_codes: tuple[BoundedText, ...] = ()


class LessonControlDecision(AgentLearningModel):
    state: LessonControlState
    changed: bool
    selectable: bool
    reason_codes: tuple[BoundedText, ...] = ()


class LessonUse(AgentLearningModel):
    candidate_id: UUID
    promoted_version_id: UUID
    title: BoundedText
    procedure: str


class LessonPromotionRegistry:
    """Fail closed unless protected evidence satisfies every M5 threshold."""

    def __init__(
        self,
        database: TenantDatabase,
        current_source_hash: str | None,
        *,
        contract_version: str = "1.0.0",
        dataset_version: str = "1.0.0",
    ) -> None:
        if current_source_hash is not None and len(current_source_hash) != 64:
            raise ValueError("a 64-character source hash is required")
        self.database = database
        self.current_source_hash = current_source_hash
        self.contract_version = contract_version
        self.dataset_version = dataset_version
        # ponytail: process-local controls are sufficient for the current
        # single-instance service; persist them before horizontal scaling.
        self._history: dict[UUID, tuple[LessonControlState, ...]] = {}
        self._promoted: dict[UUID, LessonVersion] = {}

    def promote(
        self,
        scope: LessonScope,
        evidence: LessonPromotionEvidence,
        canary_percent: int = 10,
    ) -> LessonControlDecision:
        if not 1 <= canary_percent <= 100:
            raise ValueError("canary percent must be between 1 and 100")
        evaluated = self._evaluated(scope, evidence)
        current = self._current_or_shadow(evaluated)
        if current.mode in {"canary", "active", "paused"}:
            return self._unchanged(current, ("already_promoted",))
        failures = self._evidence_failures(evidence)
        if failures:
            return self._unchanged(current, failures)

        promoted = evaluated.model_copy(
            update={
                "id": uuid4(),
                "version_number": evaluated.version_number + 1,
                "stage": "promoted",
            }
        )
        AgentLearningStore(self.database)._save_lesson_version(promoted)
        self._promoted[evidence.candidate_id] = promoted
        return self._append(
            current,
            "canary",
            (),
            promoted_version_id=promoted.id,
            evidence_id=evidence.evidence_id,
            canary_percent=canary_percent,
        )

    def monitor(
        self,
        scope: LessonScope,
        candidate_id: UUID,
        metrics: LessonMonitoringMetrics,
    ) -> LessonControlDecision:
        current = self._scoped_current(scope, candidate_id)
        if current.mode not in {"canary", "active"}:
            return self._unchanged(current, ("not_selectable",))
        failures = self._metric_failures(metrics)
        if failures:
            return self._append(
                current,
                "paused",
                tuple(f"monitor_{reason}" for reason in failures),
            )
        if current.mode == "canary":
            return self._append(current, "active", ())
        return self._unchanged(current, ())

    def rollback(
        self,
        scope: LessonScope,
        candidate_id: UUID,
        reason: str,
    ) -> LessonControlDecision:
        if not reason.strip():
            raise ValueError("rollback reason is required")
        current = self._scoped_current(scope, candidate_id)
        if current.mode == "rolled_back":
            return self._unchanged(current, ("already_rolled_back",))
        return self._append(current, "rolled_back", (reason,), canary_percent=0)

    def select(
        self,
        scope: LessonScope,
        candidate_id: UUID,
        canary_key: str,
    ) -> LessonUse | None:
        current = self._scoped_current(scope, candidate_id)
        if current.mode == "active":
            selected = True
        elif current.mode == "canary":
            bucket = int.from_bytes(
                sha256(f"{candidate_id}:{canary_key}".encode()).digest()[:4],
                "big",
            ) % 100
            selected = bucket < current.canary_percent
        else:
            selected = False
        if not selected:
            return None
        promoted = self._promoted[candidate_id]
        return LessonUse(
            candidate_id=candidate_id,
            promoted_version_id=promoted.id,
            title=promoted.title,
            procedure=promoted.procedure,
        )

    def current(self, scope: LessonScope, candidate_id: UUID) -> LessonControlState:
        return self._scoped_current(scope, candidate_id)

    def history(
        self, scope: LessonScope, candidate_id: UUID
    ) -> tuple[LessonControlState, ...]:
        self._scoped_current(scope, candidate_id)
        return self._history[candidate_id]

    def _evaluated(
        self, scope: LessonScope, evidence: LessonPromotionEvidence
    ) -> LessonVersion:
        with self.database.transaction(scope.tenant_id) as connection:
            row = connection.execute(
                text(
                    """
                    SELECT v.*
                    FROM agent_lesson_versions v
                    WHERE v.id = :version_id
                      AND v.candidate_id = :candidate_id
                      AND v.workspace_id = :workspace_id
                      AND v.agent_id = :agent_id
                      AND v.task_type = :task_type
                      AND v.environment = :environment
                      AND v.tool_name = :tool_name
                      AND v.tool_version = :tool_version
                      AND v.stage = 'evaluated'
                    """
                ),
                {
                    "version_id": evidence.evaluated_version_id,
                    "candidate_id": evidence.candidate_id,
                    "workspace_id": scope.workspace_id,
                    "agent_id": scope.agent_id,
                    "task_type": scope.task_type,
                    "environment": scope.environment,
                    "tool_name": scope.tool.name,
                    "tool_version": scope.tool.version,
                },
            ).mappings().one_or_none()
        if row is None:
            raise LessonPromotionNotFound
        return LessonVersion(
            id=row["id"],
            candidate_id=row["candidate_id"],
            scope=scope,
            version_number=row["version_number"],
            stage=row["stage"],
            title=row["title"],
            procedure=row["procedure"],
            evaluation_reference=row["evaluation_reference"],
            evaluation_contract_version=row["evaluation_contract_version"],
            evaluation_dataset_version=row["evaluation_dataset_version"],
            source_hash=row["source_hash"],
            generator=LessonModelIdentity(
                provider=row["generator_provider"],
                name=row["generator_name"],
                version=row["generator_version"],
            ),
        )

    def _current_or_shadow(self, evaluated: LessonVersion) -> LessonControlState:
        if evaluated.candidate_id not in self._history:
            self._history[evaluated.candidate_id] = (
                LessonControlState(
                    candidate_id=evaluated.candidate_id,
                    scope=evaluated.scope,
                    control_version=1,
                    mode="shadow",
                    evaluated_version_id=evaluated.id,
                    reason_codes=("not_promoted",),
                ),
            )
        return self._history[evaluated.candidate_id][-1]

    def _scoped_current(
        self, scope: LessonScope, candidate_id: UUID
    ) -> LessonControlState:
        try:
            current = self._history[candidate_id][-1]
        except KeyError as error:
            raise LessonPromotionNotFound from error
        if current.scope != scope:
            raise LessonPromotionNotFound
        return current

    def _append(
        self,
        current: LessonControlState,
        mode: Literal["canary", "active", "paused", "rolled_back"],
        reasons: tuple[str, ...],
        *,
        promoted_version_id: UUID | None = None,
        evidence_id: str | None = None,
        canary_percent: int | None = None,
    ) -> LessonControlDecision:
        state = LessonControlState(
            candidate_id=current.candidate_id,
            scope=current.scope,
            control_version=current.control_version + 1,
            mode=mode,
            evaluated_version_id=current.evaluated_version_id,
            promoted_version_id=promoted_version_id or current.promoted_version_id,
            evidence_id=evidence_id or current.evidence_id,
            canary_percent=(
                current.canary_percent if canary_percent is None else canary_percent
            ),
            previous_control_version=current.control_version,
            reason_codes=reasons,
        )
        self._history[current.candidate_id] += (state,)
        return LessonControlDecision(
            state=state,
            changed=True,
            selectable=mode in {"canary", "active"},
            reason_codes=reasons,
        )

    @staticmethod
    def _unchanged(
        state: LessonControlState, reasons: tuple[str, ...]
    ) -> LessonControlDecision:
        return LessonControlDecision(
            state=state,
            changed=False,
            selectable=state.mode in {"canary", "active"},
            reason_codes=reasons,
        )

    def _evidence_failures(
        self, evidence: LessonPromotionEvidence
    ) -> tuple[str, ...]:
        checks = {
            "source_identity": self.current_source_hash is not None,
            "contract_version": evidence.contract_version == self.contract_version,
            "dataset_version": evidence.dataset_version == self.dataset_version,
            "source_hash": evidence.source_hash == self.current_source_hash,
            "baseline": evidence.baseline_measured,
            "protected_holdout": evidence.protected_holdout_passed,
            "approval": bool(evidence.approved_by and evidence.approval_reference),
            "rollback": bool(evidence.rollback_reference),
        }
        failures = tuple(name for name, passed in checks.items() if not passed)
        return failures + self._metric_failures(evidence.metrics)

    @staticmethod
    def _metric_failures(metrics: LessonMonitoringMetrics) -> tuple[str, ...]:
        checks = {
            "minimum_pairs": metrics.pair_count >= 12,
            "quality_delta": metrics.task_quality_delta_lower_confidence_bound > 0,
            "task_success": metrics.candidate_task_success_rate >= 0.8,
            "repeated_error": metrics.repeated_failure_rate_regression <= 0,
            "applicability": metrics.applicable_lesson_use_rate >= 0.95,
            "non_applicable_use": metrics.non_applicable_lesson_use_count == 0,
            "scope_mismatch": metrics.scope_mismatch_use_count == 0,
            "critical_safety": metrics.critical_safety_regression_count == 0,
            "policy_override": metrics.policy_override_count == 0,
            "private_content": metrics.credential_or_private_reasoning_persistence_count
            == 0,
            "cross_tenant_use": metrics.cross_tenant_lesson_use_count == 0,
            "latency": metrics.p95_latency_ms <= 15_000,
            "latency_ratio": metrics.p95_vs_baseline_ratio <= 1.5,
            "cost": metrics.usd_per_1000_tasks <= 25,
            "cost_ratio": metrics.cost_vs_baseline_ratio <= 1.5,
        }
        return tuple(name for name, passed in checks.items() if not passed)


__all__ = [
    "LessonControlDecision",
    "LessonControlState",
    "LessonMonitoringMetrics",
    "LessonPromotionEvidence",
    "LessonPromotionNotFound",
    "LessonPromotionRegistry",
    "LessonUse",
]

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from memory_ops.agent_learning import (
    AgentLearningStore,
    AgentRunScope,
    CandidateLesson,
    LessonEvidenceReference,
    LessonModelIdentity,
    LessonMonitoringMetrics,
    LessonPromotionEvidence,
    LessonPromotionRegistry,
    LessonScope,
    LessonVersion,
    StructuredEpisode,
    ToolIdentity,
)
from memory_ops.agent_learning.promotion import LessonPromotionNotFound
from memory_ops.api import create_app
from memory_ops.api.agent_learning import (
    LessonScopeBody,
    MonitorLessonBody,
    PromoteLessonBody,
    RollbackLessonBody,
)
from memory_ops.config import Settings
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    SecurityBoundary,
    WorkspaceGrant,
)


SOURCE_HASH = "b" * 64
MODEL = LessonModelIdentity(provider="local", name="lesson-generator", version="1.0")


@pytest.fixture(scope="module")
def promotion_context() -> tuple[Engine, TenantDatabase, UUID, UUID]:
    settings = Settings.from_environment()
    engine = create_database_engine(settings.database_url)
    upgrade_database(engine)
    tenant_id, workspace_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:id)"), {"id": tenant_id}
        )
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant)"),
            {"id": workspace_id, "tenant": tenant_id},
        )
    yield engine, TenantDatabase(engine), tenant_id, workspace_id
    engine.dispose()


def lesson_scope(tenant_id: UUID, workspace_id: UUID) -> LessonScope:
    return LessonScope(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        agent_id=uuid4(),
        task_type="python-maintenance",
        environment="test",
        tool=ToolIdentity(name="pytest", version="9.0"),
    )


def evaluated_lesson(
    database: TenantDatabase, scope: LessonScope
) -> tuple[CandidateLesson, LessonVersion]:
    store = AgentLearningStore(database)
    run_id = uuid4()
    episode = StructuredEpisode(
        scope=AgentRunScope(
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            agent_id=scope.agent_id,
            run_id=run_id,
            task_type=scope.task_type,
            environment=scope.environment,
        ),
        episode_type="error",
        action="Run the focused test",
        outcome="The empty record was accepted",
        outcome_status="failure",
        tool=scope.tool,
    )
    store.save_episode(episode)
    candidate = CandidateLesson(
        scope=scope,
        title="Guard empty records",
        procedure="Add a focused failing test before changing the shared parser.",
        evidence=(LessonEvidenceReference(episode_id=episode.id, run_id=run_id),),
        generator=MODEL,
    )
    store.save_candidate_lesson(candidate)
    version = LessonVersion(
        candidate_id=candidate.id,
        scope=scope,
        version_number=1,
        title=candidate.title,
        procedure=candidate.procedure,
        evaluation_reference="m5-development-result-001",
        evaluation_contract_version="1.0.0",
        evaluation_dataset_version="1.0.0",
        source_hash=SOURCE_HASH,
        generator=MODEL,
    )
    store.save_evaluated_lesson(version)
    return candidate, version


def metrics(**changes: object) -> LessonMonitoringMetrics:
    values = {
        "pair_count": 12,
        "task_quality_delta_lower_confidence_bound": 0.05,
        "candidate_task_success_rate": 0.9,
        "repeated_failure_rate_regression": 0.0,
        "applicable_lesson_use_rate": 1.0,
        "non_applicable_lesson_use_count": 0,
        "scope_mismatch_use_count": 0,
        "critical_safety_regression_count": 0,
        "policy_override_count": 0,
        "credential_or_private_reasoning_persistence_count": 0,
        "cross_tenant_lesson_use_count": 0,
        "p95_latency_ms": 10_000,
        "p95_vs_baseline_ratio": 1.2,
        "usd_per_1000_tasks": 20.0,
        "cost_vs_baseline_ratio": 1.2,
    }
    values.update(changes)
    return LessonMonitoringMetrics.model_validate(values)


def evidence(
    candidate: CandidateLesson,
    version: LessonVersion,
    **changes: object,
) -> LessonPromotionEvidence:
    values = {
        "evidence_id": f"evidence-{uuid4()}",
        "candidate_id": candidate.id,
        "evaluated_version_id": version.id,
        "contract_version": "1.0.0",
        "dataset_version": "1.0.0",
        "source_hash": SOURCE_HASH,
        "baseline_measured": True,
        "protected_holdout_passed": True,
        "approved_by": "reviewer-1",
        "approval_reference": "approval-1",
        "rollback_reference": "rollback-plan-1",
        "metrics": metrics(),
    }
    values.update(changes)
    return LessonPromotionEvidence.model_validate(values)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"approved_by": None}, "approval"),
        ({"protected_holdout_passed": False}, "protected_holdout"),
        ({"rollback_reference": None}, "rollback"),
        ({"source_hash": "c" * 64}, "source_hash"),
        ({"metrics": metrics(task_quality_delta_lower_confidence_bound=0)}, "quality_delta"),
        ({"metrics": metrics(critical_safety_regression_count=1)}, "critical_safety"),
        ({"metrics": metrics(scope_mismatch_use_count=1)}, "scope_mismatch"),
        ({"metrics": metrics(cost_vs_baseline_ratio=1.6)}, "cost_ratio"),
    ],
)
def test_promotion_fails_closed_without_complete_protected_evidence(
    promotion_context: tuple[Engine, TenantDatabase, UUID, UUID],
    change: dict[str, object],
    reason: str,
) -> None:
    _, database, tenant_id, workspace_id = promotion_context
    scope = lesson_scope(tenant_id, workspace_id)
    candidate, version = evaluated_lesson(database, scope)
    registry = LessonPromotionRegistry(database, SOURCE_HASH)

    decision = registry.promote(scope, evidence(candidate, version, **change))

    assert decision.changed is False
    assert decision.selectable is False
    assert decision.state.mode == "shadow"
    assert reason in decision.reason_codes


def test_canary_monitoring_pause_and_rollback_control_selection(
    promotion_context: tuple[Engine, TenantDatabase, UUID, UUID],
) -> None:
    _, database, tenant_id, workspace_id = promotion_context
    scope = lesson_scope(tenant_id, workspace_id)
    candidate, version = evaluated_lesson(database, scope)
    registry = LessonPromotionRegistry(database, SOURCE_HASH)

    promoted = registry.promote(
        scope, evidence(candidate, version), canary_percent=25
    )

    assert promoted.state.mode == "canary"
    assert promoted.state.promoted_version_id is not None
    with database.transaction(tenant_id) as connection:
        stages = connection.execute(
            text(
                "SELECT stage FROM agent_lesson_versions "
                "WHERE candidate_id = :id ORDER BY version_number"
            ),
            {"id": candidate.id},
        ).scalars().all()
    assert stages == ["evaluated", "promoted"]
    assert any(
        registry.select(scope, candidate.id, f"run-{index}") is not None
        for index in range(100)
    )
    assert any(
        registry.select(scope, candidate.id, f"run-{index}") is None
        for index in range(100)
    )
    with pytest.raises(LessonPromotionNotFound):
        registry.select(
            scope.model_copy(update={"agent_id": uuid4()}), candidate.id, "run-1"
        )

    activated = registry.monitor(scope, candidate.id, metrics())
    assert activated.state.mode == "active"
    assert registry.select(scope, candidate.id, "every-run") is not None

    paused = registry.monitor(
        scope, candidate.id, metrics(policy_override_count=1)
    )
    assert paused.state.mode == "paused"
    assert paused.reason_codes == ("monitor_policy_override",)
    assert registry.select(scope, candidate.id, "every-run") is None

    rolled_back = registry.rollback(scope, candidate.id, "quality_regression")
    assert rolled_back.state.mode == "rolled_back"
    assert rolled_back.state.previous_control_version == 4
    assert registry.select(scope, candidate.id, "every-run") is None
    assert [state.mode for state in registry.history(scope, candidate.id)] == [
        "shadow",
        "canary",
        "active",
        "paused",
        "rolled_back",
    ]


def test_authorized_control_api_promotes_monitors_and_rolls_back(
    promotion_context: tuple[Engine, TenantDatabase, UUID, UUID],
) -> None:
    engine, database, tenant_id, workspace_id = promotion_context
    scope = lesson_scope(tenant_id, workspace_id)
    candidate, version = evaluated_lesson(database, scope)
    registry = LessonPromotionRegistry(database, SOURCE_HASH)
    principal = AuthenticatedPrincipal(
        tenant_id=tenant_id,
        principal_id=uuid4(),
        workspace_grants=(
            WorkspaceGrant(
                workspace_id,
                frozenset({"lesson:promote", "lesson:monitor", "lesson:rollback"}),
            ),
        ),
    )
    security = SecurityBoundary(
        credential_resolver=lambda credential: principal if credential == "valid" else None,
        policy=MachinePolicy(
            "policy-1",
            frozenset({"lesson:promote", "lesson:monitor", "lesson:rollback"}),
        ),
    )
    app = create_app(
        Settings.from_environment().model_copy(update={"environment": "test"}),
        security,
        database,
        registry,
    )
    base = (
        f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}"
        f"/agent-lessons/{candidate.id}"
    )
    scope_body = LessonScopeBody(
        agent_id=scope.agent_id,
        task_type=scope.task_type,
        environment=scope.environment,
        tool=scope.tool,
    )
    promotion_body = PromoteLessonBody(
        scope=scope_body,
        evidence=evidence(candidate, version),
        canary_percent=20,
    )
    headers = {"Authorization": "Bearer valid"}

    with TestClient(app) as client:
        denied = client.post(
            f"{base}/promotions",
            json=promotion_body.model_dump(mode="json"),
        )
        promoted = client.post(
            f"{base}/promotions",
            json=promotion_body.model_dump(mode="json"),
            headers=headers,
        )
        monitored = client.post(
            f"{base}/monitoring",
            json=MonitorLessonBody(scope=scope_body, metrics=metrics()).model_dump(
                mode="json"
            ),
            headers=headers,
        )
        rolled_back = client.post(
            f"{base}/rollback",
            json=RollbackLessonBody(
                scope=scope_body, reason="operator_rollback"
            ).model_dump(mode="json"),
            headers=headers,
        )

    assert denied.status_code == 401
    assert promoted.status_code == 200
    assert promoted.json()["mode"] == "canary"
    assert monitored.status_code == 200
    assert monitored.json()["mode"] == "active"
    assert rolled_back.status_code == 200
    assert rolled_back.json()["mode"] == "rolled_back"

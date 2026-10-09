"""Seed one evaluated lesson and serve the M5-04 API in Swagger."""

import json
from hashlib import sha256
from uuid import uuid4

import uvicorn
from sqlalchemy import text

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
from memory_ops.api import create_app
from memory_ops.api.agent_learning import (
    LessonScopeBody,
    MonitorLessonBody,
    PromoteLessonBody,
    RollbackLessonBody,
)
from memory_ops.config import Settings
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database


SOURCE_HASH = sha256(b"m5-04-manual-swagger").hexdigest()


def passing_metrics() -> LessonMonitoringMetrics:
    return LessonMonitoringMetrics(
        pair_count=12,
        task_quality_delta_lower_confidence_bound=0.05,
        candidate_task_success_rate=0.9,
        repeated_failure_rate_regression=0,
        applicable_lesson_use_rate=1,
        p95_latency_ms=10_000,
        p95_vs_baseline_ratio=1.2,
        usd_per_1000_tasks=20,
        cost_vs_baseline_ratio=1.2,
    )


def main() -> None:
    settings = Settings.from_environment()
    if not all(
        (
            settings.api_token,
            settings.api_tenant_id,
            settings.api_workspace_id,
            settings.api_principal_id,
        )
    ):
        raise SystemExit(
            "Set MEMORY_OPS_API_TOKEN, MEMORY_OPS_API_TENANT_ID, "
            "MEMORY_OPS_API_WORKSPACE_ID, and MEMORY_OPS_API_PRINCIPAL_ID "
            "in .env.test."
        )

    tenant_id = settings.api_tenant_id
    workspace_id = settings.api_workspace_id
    engine = create_database_engine(settings.database_url)
    upgrade_database(engine)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:id) ON CONFLICT DO NOTHING"),
            {"id": tenant_id},
        )
        connection.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant_id) "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": workspace_id, "tenant_id": tenant_id},
        )

    database = TenantDatabase(engine)
    store = AgentLearningStore(database)
    run_id = uuid4()
    scope = LessonScope(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        agent_id=uuid4(),
        task_type="swagger-manual-test",
        environment="test",
        tool=ToolIdentity(name="swagger-ui", version="1.0"),
    )
    episode = StructuredEpisode(
        scope=AgentRunScope(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            agent_id=scope.agent_id,
            run_id=run_id,
            task_type=scope.task_type,
            environment=scope.environment,
        ),
        episode_type="error",
        action="Run the manual Swagger promotion flow",
        outcome="The lesson needs protected promotion evidence",
        outcome_status="failure",
        tool=scope.tool,
    )
    store.save_episode(episode)
    model = LessonModelIdentity(
        provider="local", name="manual-swagger-seed", version="1.0"
    )
    candidate = CandidateLesson(
        scope=scope,
        title="Verify protected promotion",
        procedure="Promote only after protected evidence passes every gate.",
        evidence=(LessonEvidenceReference(episode_id=episode.id, run_id=run_id),),
        generator=model,
    )
    store.save_candidate_lesson(candidate)
    version = LessonVersion(
        candidate_id=candidate.id,
        scope=scope,
        version_number=1,
        title=candidate.title,
        procedure=candidate.procedure,
        evaluation_reference="manual-swagger-evaluation",
        evaluation_contract_version="1.0.0",
        evaluation_dataset_version="1.0.0",
        source_hash=SOURCE_HASH,
        generator=model,
    )
    store.save_evaluated_lesson(version)

    metrics = passing_metrics()
    scope_body = LessonScopeBody(
        agent_id=scope.agent_id,
        task_type=scope.task_type,
        environment=scope.environment,
        tool=scope.tool,
    )
    promote = PromoteLessonBody(
        scope=scope_body,
        evidence=LessonPromotionEvidence(
            evidence_id=f"manual-{uuid4()}",
            candidate_id=candidate.id,
            evaluated_version_id=version.id,
            contract_version="1.0.0",
            dataset_version="1.0.0",
            source_hash=SOURCE_HASH,
            baseline_measured=True,
            protected_holdout_passed=True,
            approved_by="manual-reviewer",
            approval_reference="manual-approval",
            rollback_reference="manual-rollback-plan",
            metrics=metrics,
        ),
        canary_percent=20,
    )
    requests = {
        "promotions": promote.model_dump(mode="json"),
        "monitoring": MonitorLessonBody(
            scope=scope_body, metrics=metrics
        ).model_dump(mode="json"),
        "rollback": RollbackLessonBody(
            scope=scope_body, reason="manual_operator_rollback"
        ).model_dump(mode="json"),
    }
    base_path = (
        f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}"
        f"/agent-lessons/{candidate.id}"
    )
    print("\nOpen http://127.0.0.1:8000/docs", flush=True)
    print("Authorize with MEMORY_OPS_API_TOKEN from .env.test (token only).", flush=True)
    print(f"\nBase path:\n{base_path}", flush=True)
    for route, body in requests.items():
        print(f"\nPOST {base_path}/{route}\n{json.dumps(body, indent=2)}", flush=True)

    registry = LessonPromotionRegistry(database, SOURCE_HASH)
    app = create_app(settings, database=database, lesson_promotions=registry)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()

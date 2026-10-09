from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from memory_ops.agent_learning import (
    AgentLearningStore,
    AgentRunScope,
    CandidateLesson,
    LessonEvidenceReference,
    LessonModelIdentity,
    LessonScope,
    LessonVersion,
    StructuredEpisode,
    ToolIdentity,
)
from memory_ops.config import Settings
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.security import ProhibitedContent


MODEL = LessonModelIdentity(provider="local", name="lesson-generator", version="1.0")
SOURCE_HASH = "a" * 64


@pytest.fixture(scope="module")
def lesson_context() -> tuple[Engine, TenantDatabase, AgentRunScope, UUID]:
    settings = Settings.from_environment()
    engine = create_database_engine(settings.database_url)
    upgrade_database(engine)
    tenant_id, other_tenant_id = uuid4(), uuid4()
    workspace_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:tenant), (:other)"),
            {"tenant": tenant_id, "other": other_tenant_id},
        )
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:workspace, :tenant)"),
            {"workspace": workspace_id, "tenant": tenant_id},
        )
    database = TenantDatabase(engine)
    run_scope = AgentRunScope(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        agent_id=uuid4(),
        run_id=uuid4(),
        task_type="python-maintenance",
        environment="test",
    )
    yield engine, database, run_scope, other_tenant_id
    engine.dispose()


def lesson_scope(run_scope: AgentRunScope) -> LessonScope:
    return LessonScope(
        tenant_id=run_scope.tenant_id,
        workspace_id=run_scope.workspace_id,
        agent_id=run_scope.agent_id,
        task_type=run_scope.task_type,
        environment=run_scope.environment,
        tool=ToolIdentity(name="pytest", version="9.0"),
    )


def evidence_episode(store: AgentLearningStore, scope: AgentRunScope) -> StructuredEpisode:
    episode = StructuredEpisode(
        scope=scope,
        episode_type="error",
        action="Run the focused test",
        outcome="The assertion failed because the empty record was accepted",
        outcome_status="failure",
        tool=ToolIdentity(name="pytest", version="9.0"),
    )
    store.save_episode(episode)
    return episode


def candidate_for(scope: LessonScope, episode: StructuredEpisode) -> CandidateLesson:
    return CandidateLesson(
        scope=scope,
        title="Guard empty parser records",
        procedure="Add a focused failing test before changing the shared parser.",
        evidence=(
            LessonEvidenceReference(episode_id=episode.id, run_id=episode.scope.run_id),
        ),
        generator=MODEL,
    )


def test_candidate_lesson_requires_scope_matched_episode_evidence(
    lesson_context: tuple[Engine, TenantDatabase, AgentRunScope, UUID],
) -> None:
    _, database, run_scope, _ = lesson_context
    store = AgentLearningStore(database)
    episode = evidence_episode(store, run_scope)
    candidate = candidate_for(lesson_scope(run_scope), episode)

    assert store.save_candidate_lesson(candidate) == candidate.id
    with database.transaction(run_scope.tenant_id) as connection:
        row = connection.execute(
            text(
                """
                SELECT c.title, c.procedure, c.generator_provider,
                       c.generator_name, c.generator_version, e.episode_id
                FROM agent_lesson_candidates c
                JOIN agent_lesson_candidate_evidence e
                  ON e.candidate_id = c.id AND e.tenant_id = c.tenant_id
                WHERE c.id = :id
                """
            ),
            {"id": candidate.id},
        ).one()
    assert tuple(row) == (
        "Guard empty parser records",
        "Add a focused failing test before changing the shared parser.",
        "local",
        "lesson-generator",
        "1.0",
        episode.id,
    )

    mismatched = candidate_for(
        lesson_scope(run_scope).model_copy(update={"tool": ToolIdentity(name="ruff", version="1.0")}),
        episode,
    )
    with pytest.raises(DBAPIError):
        store.save_candidate_lesson(mismatched)


def test_evaluated_versions_are_separate_immutable_and_source_bound(
    lesson_context: tuple[Engine, TenantDatabase, AgentRunScope, UUID],
) -> None:
    engine, database, run_scope, other_tenant_id = lesson_context
    store = AgentLearningStore(database)
    episode = evidence_episode(store, run_scope.model_copy(update={"run_id": uuid4()}))
    scope = lesson_scope(run_scope)
    candidate = candidate_for(scope, episode)
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

    assert store.save_evaluated_lesson(version) == version.id
    with database.transaction(run_scope.tenant_id) as connection:
        row = connection.execute(
            text(
                """
                SELECT stage, version_number, evaluation_reference,
                       evaluation_contract_version, evaluation_dataset_version,
                       source_hash
                FROM agent_lesson_versions WHERE id = :id
                """
            ),
            {"id": version.id},
        ).one()
    assert tuple(row) == (
        "evaluated",
        1,
        "m5-development-result-001",
        "1.0.0",
        "1.0.0",
        SOURCE_HASH,
    )
    with database.transaction(other_tenant_id) as connection:
        assert connection.execute(text("SELECT count(*) FROM agent_lesson_versions")).scalar_one() == 0
    with pytest.raises(DBAPIError, match="agent learning records are immutable"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE agent_lesson_versions SET stage = 'promoted' WHERE id = :id"),
                {"id": version.id},
            )


def test_candidate_content_and_premature_promotion_fail_closed(
    lesson_context: tuple[Engine, TenantDatabase, AgentRunScope, UUID],
) -> None:
    _, database, run_scope, _ = lesson_context
    store = AgentLearningStore(database)
    episode = evidence_episode(store, run_scope.model_copy(update={"run_id": uuid4()}))
    scope = lesson_scope(run_scope)

    with pytest.raises(ProhibitedContent):
        CandidateLesson(
            scope=scope,
            title="Store access",
            procedure="Persist api_key=do-not-store-this-value for later runs.",
            evidence=(
                LessonEvidenceReference(episode_id=episode.id, run_id=episode.scope.run_id),
            ),
            generator=MODEL,
        )
    with pytest.raises(ValidationError, match="String should match pattern"):
        LessonVersion(
            candidate_id=uuid4(),
            scope=scope,
            version_number=1,
            title="Invalid evidence",
            procedure="Run the focused test.",
            evaluation_reference="result-1",
            evaluation_contract_version="1.0.0",
            evaluation_dataset_version="1.0.0",
            source_hash="not-a-source-hash",
            generator=MODEL,
        )

    promoted = LessonVersion(
        candidate_id=uuid4(),
        scope=scope,
        version_number=1,
        stage="promoted",
        title="Premature promotion",
        procedure="Run the focused test.",
        evaluation_reference="result-1",
        evaluation_contract_version="1.0.0",
        evaluation_dataset_version="1.0.0",
        source_hash=SOURCE_HASH,
        generator=MODEL,
    )
    with pytest.raises(ValueError, match="promotion requires"):
        store.save_evaluated_lesson(promoted)

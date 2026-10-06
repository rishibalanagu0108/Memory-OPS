from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from memory_ops.config import Settings
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.retrieval import RetrievalService
from memory_ops.user_memory import (
    CorrectionRequest,
    MemoryScope,
    RememberRequest,
    UserMemoryService,
)


@pytest.fixture(scope="module")
def retrieval_store() -> tuple[Engine, RetrievalService, UserMemoryService, MemoryScope, MemoryScope]:
    settings = Settings.from_environment()
    engine = create_database_engine(settings.database_url)
    upgrade_database(engine)
    tenant_a, tenant_b = uuid4(), uuid4()
    workspace_a, workspace_b = uuid4(), uuid4()
    subject_a, subject_b = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:a), (:b)"),
            {"a": tenant_a, "b": tenant_b},
        )
        connection.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id) "
                "VALUES (:workspace_a, :tenant_a), (:workspace_b, :tenant_b)"
            ),
            {
                "workspace_a": workspace_a,
                "tenant_a": tenant_a,
                "workspace_b": workspace_b,
                "tenant_b": tenant_b,
            },
        )
    scope_a = MemoryScope(
        tenant_id=tenant_a,
        workspace_id=workspace_a,
        subject_id=subject_a,
    )
    scope_b = MemoryScope(
        tenant_id=tenant_b,
        workspace_id=workspace_b,
        subject_id=subject_b,
    )
    database = TenantDatabase(engine)
    yield engine, RetrievalService(database), UserMemoryService(database, "policy-2026-10"), scope_a, scope_b
    engine.dispose()


def remember(
    service: UserMemoryService,
    scope: MemoryScope,
    statement: str,
    *,
    semantic_type: str = "fact",
    purpose: str = "planning",
    access_scope: tuple[str, ...] = (),
    retention_until: datetime | None = None,
) -> UUID:
    return service.remember(
        RememberRequest(
            scope=scope,
            semantic_type=semantic_type,
            statement=statement,
            purpose=purpose,
            access_scope=access_scope,
            retention_until=retention_until,
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        f"remember-{uuid4()}",
    ).receipt.resource_id


def test_exact_retrieval_requires_the_authorized_scope_and_purpose(
    retrieval_store: tuple[Engine, RetrievalService, UserMemoryService, MemoryScope, MemoryScope],
) -> None:
    _, retrieval, writer, scope_a, scope_b = retrieval_store
    memory_id = remember(writer, scope_a, "The Atlas deadline is 18 October 2026.")

    candidate = retrieval.exact(scope_a, memory_id, purpose="planning")

    assert candidate is not None
    assert candidate.memory_id == memory_id
    assert candidate.channel == "exact"
    assert retrieval.exact(scope_a, memory_id, purpose="travel") is None
    assert retrieval.exact(scope_b, memory_id, purpose="planning") is None


def test_exact_retrieval_enforces_subject_and_agent_boundaries(
    retrieval_store: tuple[Engine, RetrievalService, UserMemoryService, MemoryScope, MemoryScope],
) -> None:
    _, retrieval, writer, scope, _ = retrieval_store
    agent_id = uuid4()
    agent_scope = scope.model_copy(update={"agent_id": agent_id})
    memory_id = remember(writer, agent_scope, "Agent-specific deployment preference.")

    assert retrieval.exact(agent_scope, memory_id, purpose="planning") is not None
    assert retrieval.exact(
        scope.model_copy(update={"agent_id": uuid4()}),
        memory_id,
        purpose="planning",
    ) is None
    assert retrieval.exact(
        agent_scope.model_copy(update={"subject_id": uuid4()}),
        memory_id,
        purpose="planning",
    ) is None


def test_structured_filters_enforce_semantics_and_access_scope(
    retrieval_store: tuple[Engine, RetrievalService, UserMemoryService, MemoryScope, MemoryScope],
) -> None:
    _, retrieval, writer, scope, _ = retrieval_store
    public_id = remember(writer, scope, "Keep cloud spend below 100 dollars.", semantic_type="goal")
    restricted_id = remember(
        writer,
        scope,
        "The private launch budget is 500 dollars.",
        semantic_type="goal",
        access_scope=("finance-reviewer",),
    )
    remember(writer, scope, "Use type annotations.", semantic_type="preference", purpose="coding")

    public = retrieval.filtered(scope, purpose="planning", semantic_types=("goal",))
    privileged = retrieval.filtered(
        scope,
        purpose="planning",
        semantic_types=("goal",),
        access_scopes=("finance-reviewer",),
    )

    assert {item.memory_id for item in public} == {public_id}
    assert {item.memory_id for item in privileged} == {public_id, restricted_id}


def test_keyword_retrieval_ranks_matches_and_rejects_stale_or_expired_versions(
    retrieval_store: tuple[Engine, RetrievalService, UserMemoryService, MemoryScope, MemoryScope],
) -> None:
    _, retrieval, writer, scope, _ = retrieval_store
    current_id = remember(writer, scope, "Atlas project deadline and launch checklist.")
    corrected_id = remember(writer, scope, "Atlas project deadline was Friday.")
    writer.correct(
        CorrectionRequest(
            scope=scope,
            memory_id=corrected_id,
            statement="Atlas project retrospective is complete.",
        ),
        f"correct-{uuid4()}",
    )
    expired_id = remember(
        writer,
        scope,
        "Atlas project deadline from an expired note.",
        retention_until=datetime.now(UTC) - timedelta(seconds=1),
    )

    results = retrieval.keyword(scope, "Atlas project deadline", purpose="planning")

    assert results
    assert results[0].memory_id == current_id
    assert corrected_id not in {item.memory_id for item in results}
    assert expired_id not in {item.memory_id for item in results}
    assert all(item.score > 0 and item.channel == "keyword" for item in results)


def test_keyword_retrieval_rejects_blank_queries_and_invalid_limits(
    retrieval_store: tuple[Engine, RetrievalService, UserMemoryService, MemoryScope, MemoryScope],
) -> None:
    _, retrieval, _, scope, _ = retrieval_store

    assert retrieval.keyword(scope, "   ", purpose="planning") == ()
    with pytest.raises(ValueError, match="limit"):
        retrieval.filtered(scope, purpose="planning", limit=101)
    with pytest.raises(ValueError, match="query"):
        retrieval.keyword(scope, "x" * 10_001, purpose="planning")

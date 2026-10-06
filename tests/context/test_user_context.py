from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from memory_ops.api import create_app
from memory_ops.config import Settings
from memory_ops.context import UserContextService
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.retrieval import RetrievalCandidate, RetrievalService
from memory_ops.security import AuthenticatedPrincipal, MachinePolicy, SecurityBoundary, WorkspaceGrant
from memory_ops.user_memory import (
    CorrectionRequest,
    EvidenceReference,
    MemoryScope,
    RememberRequest,
    UserMemoryService,
)


@pytest.fixture(scope="module")
def context_store() -> tuple[Engine, TenantDatabase, MemoryScope]:
    engine = create_database_engine(Settings.from_environment().database_url)
    upgrade_database(engine)
    tenant_id, workspace_id, subject_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants (id) VALUES (:id)"), {"id": tenant_id})
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant_id)"),
            {"id": workspace_id, "tenant_id": tenant_id},
        )
    database = TenantDatabase(engine)
    yield engine, database, MemoryScope(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        subject_id=subject_id,
    )
    engine.dispose()


def remember(
    database: TenantDatabase,
    scope: MemoryScope,
    statement: str,
    *,
    semantic_type: str = "fact",
    retention_until: datetime | None = None,
) -> tuple[UUID, UUID]:
    result = UserMemoryService(database, "policy-2026-10").remember(
        RememberRequest(
            scope=scope,
            semantic_type=semantic_type,
            statement=statement,
            purpose="planning",
            retention_until=retention_until,
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            evidence=(EvidenceReference(evidence_type="event", reference_id=f"event-{uuid4()}"),),
        ),
        f"remember-{uuid4()}",
    )
    return result.receipt.resource_id, result.receipt.resource_version


def candidate(memory_id: UUID, version_id: UUID, statement: str, score: float = 1.0) -> RetrievalCandidate:
    return RetrievalCandidate(memory_id, version_id, statement, "fact", "planning", score, "keyword")


def test_context_hydrates_current_canonical_versions_and_filters_stale_candidates(
    context_store: tuple[Engine, TenantDatabase, MemoryScope],
) -> None:
    _, database, scope = context_store
    writer = UserMemoryService(database, "policy-2026-10")
    memory_id, old_version_id = remember(database, scope, "Atlas deadline was Friday.")
    corrected = writer.correct(
        CorrectionRequest(scope=scope, memory_id=memory_id, statement="Atlas deadline is Monday."),
        f"correct-{uuid4()}",
    )
    result = UserContextService(database).assemble(
        scope,
        (
            candidate(memory_id, old_version_id, "stale"),
            candidate(memory_id, corrected.receipt.resource_version, "current"),
        ),
        purpose="planning",
        token_budget=20,
    )

    assert result.abstained is False
    assert len(result.sections[0].items) == 1
    assert result.sections[0].items[0].version_id == corrected.receipt.resource_version
    assert result.sections[0].items[0].content == "Atlas deadline is Monday."
    assert "stale_or_unauthorized_candidates_filtered" in result.warnings


def test_constraints_are_mandatory_and_context_never_exceeds_budget(
    context_store: tuple[Engine, TenantDatabase, MemoryScope],
) -> None:
    _, database, scope = context_store
    fact_id, fact_version = remember(database, scope, "Atlas launches next Monday.")
    constraint_id, constraint_version = remember(
        database,
        scope,
        "Never deploy without approval.",
        semantic_type="constraint",
    )
    result = UserContextService(database).assemble(
        scope,
        (
            candidate(fact_id, fact_version, "fact", 2.0),
            candidate(constraint_id, constraint_version, "constraint", 1.0),
        ),
        purpose="planning",
        token_budget=7,
    )

    items = result.sections[0].items
    assert items[0].memory_id == constraint_id
    assert items[0].critical is True
    assert result.used_tokens <= result.token_budget
    assert "token_budget_exhausted" in result.warnings


def test_expired_candidates_are_removed_before_context_assembly(
    context_store: tuple[Engine, TenantDatabase, MemoryScope],
) -> None:
    _, database, scope = context_store
    memory_id, version_id = remember(
        database,
        scope,
        "Expired planning note.",
        retention_until=datetime.now(UTC) - timedelta(seconds=1),
    )

    result = UserContextService(database).assemble(
        scope,
        (candidate(memory_id, version_id, "expired"),),
        purpose="planning",
        token_budget=20,
    )

    assert result.abstained is True
    assert result.sections[0].items == ()


class UnavailableRetrieval(RetrievalService):
    def keyword(self, *args, **kwargs):
        raise OperationalError("keyword unavailable", {}, RuntimeError())

    def vector(self, *args, **kwargs):
        raise OperationalError("vector unavailable", {}, RuntimeError())


def test_optional_search_failure_returns_explicit_partial_abstention(
    context_store: tuple[Engine, TenantDatabase, MemoryScope],
) -> None:
    _, database, scope = context_store
    service = UserContextService(database, retrieval=UnavailableRetrieval(database))

    result = service.build(
        scope,
        "Atlas deadline",
        purpose="planning",
        token_budget=20,
    )

    assert result.partial is True
    assert result.abstained is True
    assert result.warnings == (
        "keyword_unavailable",
        "vector_unavailable",
        "insufficient_evidence",
    )


def test_context_api_is_authorized_and_domain_labelled(
    context_store: tuple[Engine, TenantDatabase, MemoryScope],
) -> None:
    engine, database, scope = context_store
    principal = AuthenticatedPrincipal(
        tenant_id=scope.tenant_id,
        principal_id=uuid4(),
        workspace_grants=(WorkspaceGrant(scope.workspace_id, frozenset({"memory:read"})),),
    )
    app = create_app(
        Settings.from_environment().model_copy(update={"environment": "test"}),
        SecurityBoundary(
            credential_resolver=lambda token: principal if token == "valid" else None,
            policy=MachinePolicy("policy-2026-10", frozenset({"memory:read"})),
        ),
        database,
    )
    path = f"/v1/tenants/{scope.tenant_id}/workspaces/{scope.workspace_id}/context"
    body = {
        "subject_id": str(scope.subject_id),
        "query": "unknown subject",
        "purpose": "planning",
        "token_budget": 20,
    }
    with TestClient(app) as client:
        assert client.post(path, json=body).status_code == 401
        response = client.post(path, json=body, headers={"Authorization": "Bearer valid"})

    assert response.status_code == 200
    assert response.json()["sections"][0]["domain"] == "user_memory"
    assert response.json()["abstained"] is True

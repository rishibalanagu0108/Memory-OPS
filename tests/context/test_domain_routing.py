from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from memory_ops.agent_learning import LessonUse
from memory_ops.api import create_app
from memory_ops.config import Settings
from memory_ops.context import ContextResult, ContextSection, route_context_domains
from memory_ops.knowledge.search import KnowledgeSearchResult
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    SecurityBoundary,
    WorkspaceGrant,
)


def empty_user_result() -> ContextResult:
    return ContextResult(
        sections=(ContextSection("user_memory", ()),),
        warnings=(),
        partial=False,
        abstained=True,
        used_tokens=0,
        token_budget=20,
    )


def empty_knowledge_result() -> KnowledgeSearchResult:
    return KnowledgeSearchResult((), (), partial=False, abstained=True)


def test_router_calls_each_authorized_domain_once() -> None:
    calls: list[str] = []

    def retrieve(domain, result):
        def operation():
            calls.append(domain)
            return result

        return operation

    lesson = LessonUse(
        candidate_id=uuid4(),
        promoted_version_id=uuid4(),
        title="Retry transient failures",
        procedure="Retry bounded transient failures with backoff.",
    )
    result = route_context_domains(
        user_memory=retrieve("user_memory", empty_user_result()),
        agent_learning=retrieve("agent_learning", (lesson,)),
        organizational_knowledge=retrieve(
            "organizational_knowledge", empty_knowledge_result()
        ),
    )

    assert calls == ["user_memory", "agent_learning", "organizational_knowledge"]
    assert result.agent_learning == (lesson,)
    assert [status.item_count for status in result.domains] == [0, 1, 0]
    assert result.partial is False


def test_router_isolates_one_domain_outage() -> None:
    def unavailable():
        raise OperationalError("knowledge unavailable", {}, RuntimeError())

    result = route_context_domains(
        user_memory=empty_user_result,
        agent_learning=lambda: (),
        organizational_knowledge=unavailable,
    )

    assert result.user_memory is not None
    assert result.agent_learning == ()
    assert result.organizational_knowledge is None
    assert result.partial is True
    assert result.warnings == (
        "domain_unavailable:organizational_knowledge",
    )


def test_context_request_authorizes_domains_independently(monkeypatch) -> None:
    tenant_id, workspace_id, subject_id, principal_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    actions = frozenset({"memory:read", "knowledge:read"})
    principal = AuthenticatedPrincipal(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_grants=(WorkspaceGrant(workspace_id, actions),),
    )
    boundary = SecurityBoundary(
        credential_resolver=lambda token: principal if token == "valid" else None,
        policy=MachinePolicy("policy-1", actions),
    )

    class UserService:
        def __init__(self, _database):
            pass

        def build(self, *_args, **_kwargs):
            return empty_user_result()

    class KnowledgeService:
        def __init__(self, _database):
            pass

        def search(self, *_args, **_kwargs):
            return empty_knowledge_result()

    monkeypatch.setattr("memory_ops.api.context.UserContextService", UserService)
    monkeypatch.setattr("memory_ops.api.context.KnowledgeSearchService", KnowledgeService)
    app = create_app(
        Settings.from_environment().model_copy(update={"environment": "test"}),
        boundary,
        database=object(),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.post(
            f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}/context",
            headers={"Authorization": "Bearer valid"},
            json={
                "subject_id": str(subject_id),
                "query": "incident response",
                "purpose": "planning",
                "token_budget": 20,
            },
        )

    assert response.status_code == 200
    domains = {item["domain"]: item for item in response.json()["domains"]}
    assert domains["user_memory"]["available"] is True
    assert domains["organizational_knowledge"]["available"] is True
    assert domains["agent_learning"] == {
        "domain": "agent_learning",
        "authorized": False,
        "available": False,
        "item_count": 0,
    }
    assert "domain_unauthorized:agent_learning" in response.json()["warnings"]

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from memory_ops.agent_learning import LessonUse
from memory_ops.api import create_app
from memory_ops.config import Settings
from memory_ops.context import (
    AgentContextItem,
    ContextConflict,
    ContextItem,
    ContextResult,
    ContextSection,
    KnowledgeContextItem,
    assemble_cross_domain,
    route_context_domains,
)
from memory_ops.knowledge.search import (
    KnowledgeCitation,
    KnowledgePassage,
    KnowledgeSearchResult,
)
from memory_ops.user_memory import EvidenceReference
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    SecurityBoundary,
    WorkspaceGrant,
)


def user_item(content: str, tokens: int, *, critical: bool = False) -> ContextItem:
    return ContextItem(
        domain="user_memory",
        memory_id=uuid4(),
        version_id=uuid4(),
        semantic_type="constraint" if critical else "preference",
        content=content,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_to=None,
        provenance=(
            EvidenceReference(evidence_type="event", reference_id="event-1"),
        ),
        tokens=tokens,
        critical=critical,
    )


def knowledge_result(content: str, *, warnings: tuple[str, ...] = ()) -> KnowledgeSearchResult:
    citation = KnowledgeCitation(
        document_id=uuid4(),
        document_version_id=uuid4(),
        chunk_id=uuid4(),
        source_id="policy.md",
        title="Policy",
        document_content_hash="a" * 64,
        chunk_content_hash="b" * 64,
        access_policy_version="policy-1",
        locator_kind="markdown_lines",
        locator_path="policy.md#retention",
        start_line=3,
        end_line=3,
        structure_path=("Retention",),
    )
    return KnowledgeSearchResult(
        (KnowledgePassage(content, 1.0, "untrusted", citation),),
        warnings,
        partial=False,
        abstained=False,
    )


def retrieval(
    *,
    user_items: tuple[ContextItem, ...] = (),
    lesson: LessonUse | None = None,
    knowledge: KnowledgeSearchResult | None = None,
):
    return route_context_domains(
        user_memory=lambda: ContextResult(
            sections=(ContextSection("user_memory", user_items),),
            warnings=(),
            partial=False,
            abstained=not user_items,
            used_tokens=sum(item.tokens for item in user_items),
            token_budget=100,
        ),
        agent_learning=lambda: (lesson,) if lesson else (),
        organizational_knowledge=(lambda: knowledge) if knowledge else lambda: KnowledgeSearchResult((), (), False, True),
    )


def test_assembly_preserves_domain_authority_and_provenance() -> None:
    user = user_item("The user prefers concise updates.", 5)
    lesson = LessonUse(
        candidate_id=uuid4(),
        promoted_version_id=uuid4(),
        title="Incident summary",
        procedure="State impact before remediation details.",
    )
    knowledge = knowledge_result("Reports must include impact, owner, and timeline.")

    result = assemble_cross_domain(
        retrieval(user_items=(user,), lesson=lesson, knowledge=knowledge),
        100,
    )

    assert [section.domain for section in result.sections] == [
        "user_memory",
        "agent_learning",
        "organizational_knowledge",
    ]
    assert result.sections[0].items[0].provenance == user.provenance
    assert isinstance(result.sections[1].items[0], AgentContextItem)
    assert result.sections[1].items[0].promoted_version_id == lesson.promoted_version_id
    assert isinstance(result.sections[2].items[0], KnowledgeContextItem)
    assert result.sections[2].items[0].citation == knowledge.passages[0].citation


def test_budget_prioritizes_critical_constraints_and_cited_knowledge() -> None:
    critical = user_item("Never deploy without approval.", 4, critical=True)
    preference = user_item("Prefer verbose updates.", 3)
    lesson = LessonUse(
        candidate_id=uuid4(),
        promoted_version_id=uuid4(),
        title="Deploy",
        procedure="Run the complete deployment checklist.",
    )
    knowledge = knowledge_result("Production changes require approval.")

    result = assemble_cross_domain(
        retrieval(
            user_items=(preference, critical),
            lesson=lesson,
            knowledge=knowledge,
        ),
        8,
    )

    assert result.used_tokens == 8
    assert result.sections[0].items == (critical,)
    assert result.sections[1].items == ()
    assert len(result.sections[2].items) == 1
    assert "token_budget_exhausted" in result.warnings


def test_missing_domain_stays_partial_without_discarding_available_context() -> None:
    routed = route_context_domains(
        user_memory=lambda: ContextResult(
            sections=(ContextSection("user_memory", (user_item("Keep it short.", 3),)),),
            warnings=(),
            partial=False,
            abstained=False,
            used_tokens=3,
            token_budget=20,
        ),
        agent_learning=None,
        organizational_knowledge=lambda: KnowledgeSearchResult((), (), False, True),
    )

    result = assemble_cross_domain(routed, 20)

    assert result.partial is True
    assert result.sections[0].items
    assert result.sections[1].items == ()
    assert "domain_unauthorized:agent_learning" in result.warnings


def test_conflicts_are_explicit_and_never_silently_resolved() -> None:
    conflict = ContextConflict(
        code="unresolved_conflict",
        domains=("user_memory", "organizational_knowledge"),
        item_references=("memory-retention", "policy-retention"),
    )
    routed = replace(
        retrieval(
            user_items=(user_item("Keep reports for 90 days.", 6),),
            knowledge=knowledge_result("Reports must be deleted after 30 days."),
        ),
        conflicts=(conflict,),
    )

    result = assemble_cross_domain(routed, 30)

    assert result.conflicts == (conflict,)
    assert "unresolved_conflict" in result.warnings
    assert result.sections[0].items and result.sections[2].items


def test_critical_constraints_fail_closed_when_they_exceed_budget() -> None:
    routed = retrieval(
        user_items=(user_item("Critical safety constraint.", 5, critical=True),),
    )

    result = assemble_cross_domain(routed, 4)

    assert result.abstained is True
    assert result.used_tokens == 0
    assert all(not section.items for section in result.sections)
    assert "critical_constraints_exceed_budget" in result.warnings


def test_context_api_returns_all_domain_sections_and_conflicts(monkeypatch) -> None:
    tenant_id, workspace_id, subject_id, agent_id, principal_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    actions = frozenset({"memory:read", "lesson:read", "knowledge:read"})
    principal = AuthenticatedPrincipal(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_grants=(WorkspaceGrant(workspace_id, actions),),
    )
    boundary = SecurityBoundary(
        credential_resolver=lambda token: principal if token == "valid" else None,
        policy=MachinePolicy("policy-1", actions),
    )
    user = user_item("Keep incident updates concise.", 4)
    lesson = LessonUse(
        candidate_id=uuid4(),
        promoted_version_id=uuid4(),
        title="Incident summary",
        procedure="State impact before remediation details.",
    )
    knowledge = knowledge_result(
        "Reports must include impact, owner, and timeline.",
        warnings=("unresolved_conflict",),
    )

    class UserService:
        def __init__(self, _database):
            pass

        def build(self, *_args, **_kwargs):
            return ContextResult(
                sections=(ContextSection("user_memory", (user,)),),
                warnings=(),
                partial=False,
                abstained=False,
                used_tokens=user.tokens,
                token_budget=100,
            )

    class KnowledgeService:
        def __init__(self, _database):
            pass

        def search(self, *_args, **_kwargs):
            return knowledge

    class LessonRegistry:
        def select(self, *_args, **_kwargs):
            return lesson

    monkeypatch.setattr("memory_ops.api.context.UserContextService", UserService)
    monkeypatch.setattr("memory_ops.api.context.KnowledgeSearchService", KnowledgeService)
    app = create_app(
        Settings.from_environment().model_copy(update={"environment": "test"}),
        boundary,
        database=object(),  # type: ignore[arg-type]
        lesson_promotions=LessonRegistry(),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.post(
            f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}/context",
            headers={"Authorization": "Bearer valid"},
            json={
                "subject_id": str(subject_id),
                "agent_id": str(agent_id),
                "query": "incident update",
                "purpose": "planning",
                "token_budget": 100,
                "agent_learning": {
                    "task_type": "incident-response",
                    "environment": "production",
                    "tool": {"name": "incident-api", "version": "1"},
                    "candidate_ids": [str(lesson.candidate_id)],
                    "canary_key": "run-1",
                },
            },
        )

    assert response.status_code == 200
    payload = response.json()
    sections = {section["domain"]: section["items"] for section in payload["sections"]}
    assert sections["user_memory"][0]["provenance"][0]["reference_id"] == "event-1"
    assert sections["agent_learning"][0]["promoted_version_id"] == str(
        lesson.promoted_version_id
    )
    assert sections["organizational_knowledge"][0]["citation"]["locator_path"] == (
        "policy.md#retention"
    )
    assert payload["conflicts"][0]["code"] == "unresolved_conflict"

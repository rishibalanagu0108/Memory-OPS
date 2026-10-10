"""Authorized token-budgeted context endpoint."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, Request
from pydantic import Field, model_validator

from memory_ops.agent_learning import LessonScope, ToolIdentity
from memory_ops.api.schemas import ErrorResponse, WireModel
from memory_ops.api.security import authorize_request
from memory_ops.context import (
    ContextResult,
    ContextSection,
    UserContextService,
    route_context_domains,
    select_agent_lessons,
)
from memory_ops.knowledge import KnowledgeScope
from memory_ops.knowledge.search import KnowledgeSearchService
from memory_ops.persistence import TenantDatabase
from memory_ops.security import PermissionDenied, ResourceScope, Unauthenticated
from memory_ops.user_memory import EvidenceReference, MemoryScope, SemanticType


ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


class AgentLearningContextRequest(WireModel):
    task_type: Annotated[str, Field(min_length=1, max_length=255)]
    environment: Annotated[str, Field(min_length=1, max_length=255)]
    tool: ToolIdentity
    candidate_ids: tuple[UUID, ...] = ()
    canary_key: Annotated[str, Field(min_length=1, max_length=255)]


class ContextRequest(WireModel):
    subject_id: UUID
    agent_id: UUID | None = None
    query: Annotated[str, Field(min_length=1, max_length=10_000)]
    purpose: Annotated[str, Field(min_length=1, max_length=255)]
    token_budget: Annotated[int, Field(ge=1, le=100_000)]
    access_scope: tuple[Annotated[str, Field(min_length=1, max_length=255)], ...] = ()
    agent_learning: AgentLearningContextRequest | None = None

    @model_validator(mode="after")
    def require_agent_identity_for_lessons(self) -> "ContextRequest":
        if self.agent_learning is not None and self.agent_id is None:
            raise ValueError("agent_id is required for agent learning context")
        return self


class ContextItemResponse(WireModel):
    domain: Literal["user_memory"]
    memory_id: UUID
    version_id: UUID
    semantic_type: SemanticType
    content: str
    valid_from: datetime
    valid_to: datetime | None
    provenance: tuple[EvidenceReference, ...]
    tokens: int
    critical: bool


class ContextSectionResponse(WireModel):
    domain: Literal["user_memory"]
    items: tuple[ContextItemResponse, ...]


class ContextResponse(WireModel):
    sections: tuple[ContextSectionResponse, ...]
    warnings: tuple[str, ...]
    partial: bool
    abstained: bool
    used_tokens: int
    token_budget: int
    domains: tuple["ContextDomainResponse", ...]


class ContextDomainResponse(WireModel):
    domain: Literal["user_memory", "agent_learning", "organizational_knowledge"]
    authorized: bool
    available: bool
    item_count: int


def install_context_routes(app: FastAPI) -> None:
    @app.post(
        "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/context",
        response_model=ContextResponse,
        responses=ERROR_RESPONSES,
        tags=["context"],
    )
    def build_context(
        request: Request,
        tenant_id: UUID,
        workspace_id: UUID,
        body: ContextRequest,
    ) -> ContextResponse:
        resource = ResourceScope(tenant_id, workspace_id)
        actions = {
            "user_memory": "memory:read",
            "agent_learning": "lesson:read",
            "organizational_knowledge": "knowledge:read",
        }
        authorized = {}
        for domain, action in actions.items():
            try:
                authorized[domain] = authorize_request(request, resource, action)
            except Unauthenticated:
                raise
            except PermissionDenied:
                continue
        if not authorized:
            raise PermissionDenied

        database: TenantDatabase = request.app.state.database
        memory_scope = MemoryScope(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            subject_id=body.subject_id,
            agent_id=body.agent_id,
        )
        lesson_request = body.agent_learning
        knowledge_principal = authorized.get("organizational_knowledge")
        user_retrieval = None
        if "user_memory" in authorized:
            user_retrieval = lambda: UserContextService(database).build(
                memory_scope,
                body.query,
                purpose=body.purpose,
                token_budget=body.token_budget,
                access_scopes=body.access_scope,
            )
        lesson_retrieval = None
        if "agent_learning" in authorized:
            lesson_retrieval = lambda: ()
            if lesson_request is not None:
                assert body.agent_id is not None
                lesson_retrieval = lambda: select_agent_lessons(
                    request.app.state.lesson_promotions,
                    LessonScope(
                        tenant_id=tenant_id,
                        workspace_id=workspace_id,
                        agent_id=body.agent_id,
                        task_type=lesson_request.task_type,
                        environment=lesson_request.environment,
                        tool=lesson_request.tool,
                    ),
                    lesson_request.candidate_ids,
                    lesson_request.canary_key,
                )
        knowledge_retrieval = None
        if knowledge_principal is not None:
            knowledge_retrieval = lambda: KnowledgeSearchService(database).search(
                KnowledgeScope(tenant_id=tenant_id, workspace_id=workspace_id),
                knowledge_principal.principal_id,
                body.query,
            )
        result = route_context_domains(
            user_memory=user_retrieval,
            agent_learning=lesson_retrieval,
            organizational_knowledge=knowledge_retrieval,
        )
        for domain in authorized:
            authorize_request(request, resource, actions[domain])

        user_result = result.user_memory or ContextResult(
            sections=(ContextSection("user_memory", ()),),
            warnings=(),
            partial=True,
            abstained=True,
            used_tokens=0,
            token_budget=body.token_budget,
        )
        knowledge_abstained = (
            result.organizational_knowledge is None
            or result.organizational_knowledge.abstained
        )
        return ContextResponse(
            sections=tuple(
                ContextSectionResponse.model_validate(section, from_attributes=True)
                for section in user_result.sections
            ),
            warnings=result.warnings,
            partial=result.partial,
            abstained=(
                user_result.abstained
                and not result.agent_learning
                and knowledge_abstained
            ),
            used_tokens=user_result.used_tokens,
            token_budget=body.token_budget,
            domains=tuple(
                ContextDomainResponse.model_validate(status, from_attributes=True)
                for status in result.domains
            ),
        )

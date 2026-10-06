"""Authorized token-budgeted context endpoint."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, Request
from pydantic import Field

from memory_ops.api.schemas import ErrorResponse, WireModel
from memory_ops.api.security import authorize_request
from memory_ops.context import UserContextService
from memory_ops.persistence import TenantDatabase
from memory_ops.security import ResourceScope
from memory_ops.user_memory import EvidenceReference, MemoryScope, SemanticType


ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


class ContextRequest(WireModel):
    subject_id: UUID
    agent_id: UUID | None = None
    query: Annotated[str, Field(min_length=1, max_length=10_000)]
    purpose: Annotated[str, Field(min_length=1, max_length=255)]
    token_budget: Annotated[int, Field(ge=1, le=100_000)]
    access_scope: tuple[Annotated[str, Field(min_length=1, max_length=255)], ...] = ()


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
        authorize_request(
            request,
            ResourceScope(tenant_id, workspace_id),
            "memory:read",
        )
        database: TenantDatabase = request.app.state.database
        result = UserContextService(database).build(
            MemoryScope(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                subject_id=body.subject_id,
                agent_id=body.agent_id,
            ),
            body.query,
            purpose=body.purpose,
            token_budget=body.token_budget,
            access_scopes=body.access_scope,
        )
        return ContextResponse.model_validate(result, from_attributes=True)

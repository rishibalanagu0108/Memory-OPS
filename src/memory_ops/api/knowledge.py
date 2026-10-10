"""Authorized organizational-knowledge search API."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, Request
from pydantic import Field

from memory_ops.api.schemas import ErrorResponse, WireModel
from memory_ops.api.security import authorize_request
from memory_ops.knowledge import KnowledgeScope
from memory_ops.knowledge.search import KnowledgeSearchService
from memory_ops.persistence import TenantDatabase
from memory_ops.security import ResourceScope


ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


class KnowledgeSearchBody(WireModel):
    query: Annotated[str, Field(min_length=1, max_length=10_000)]
    limit: Annotated[int, Field(ge=1, le=100)] = 10


class KnowledgeCitationResponse(WireModel):
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID
    source_id: str
    title: str
    document_content_hash: str
    chunk_content_hash: str
    access_policy_version: str
    locator_kind: str
    locator_path: str
    start_line: int
    end_line: int
    structure_path: tuple[str, ...]


class KnowledgePassageResponse(WireModel):
    content: str
    score: float
    trust: Literal["untrusted"]
    citation: KnowledgeCitationResponse


class KnowledgeSearchResponse(WireModel):
    passages: tuple[KnowledgePassageResponse, ...]
    warnings: tuple[str, ...]
    partial: bool
    abstained: bool
    retrieval_channel: Literal["keyword"]


def install_knowledge_routes(app: FastAPI) -> None:
    @app.post(
        "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/knowledge/search",
        response_model=KnowledgeSearchResponse,
        responses=ERROR_RESPONSES,
        tags=["knowledge"],
    )
    def search_knowledge(
        request: Request,
        tenant_id: UUID,
        workspace_id: UUID,
        body: KnowledgeSearchBody,
    ) -> KnowledgeSearchResponse:
        resource = ResourceScope(tenant_id, workspace_id)
        principal = authorize_request(request, resource, "knowledge:read")
        database: TenantDatabase = request.app.state.database
        result = KnowledgeSearchService(database).search(
            KnowledgeScope(tenant_id=tenant_id, workspace_id=workspace_id),
            principal.principal_id,
            body.query,
            limit=body.limit,
        )
        authorize_request(request, resource, "knowledge:read")
        return KnowledgeSearchResponse.model_validate(result, from_attributes=True)

"""Authorized HTTP entry point for non-persisting memory extraction."""

from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Request
from pydantic import Field

from memory_ops.api.security import authorize_request
from memory_ops.api.schemas import ErrorResponse, WireModel
from memory_ops.extraction import ExtractionResult, ExtractionSource, ShadowExtractionPipeline
from memory_ops.security import ResourceScope
from memory_ops.user_memory import MemoryScope, Sensitivity


class ExtractMemoryBody(WireModel):
    subject_id: UUID
    agent_id: UUID | None = None
    reference_id: Annotated[str, Field(min_length=1, max_length=255)]
    content: Annotated[str, Field(min_length=1, max_length=10_000)]
    sensitivity: Sensitivity = "normal"


def install_extraction_routes(app: FastAPI) -> None:
    @app.post(
        "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/memory-extractions",
        response_model=ExtractionResult,
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
        },
        tags=["user-memory"],
    )
    def extract_memories(
        request: Request,
        tenant_id: UUID,
        workspace_id: UUID,
        body: ExtractMemoryBody,
    ) -> ExtractionResult:
        authorize_request(
            request, ResourceScope(tenant_id, workspace_id), "memory:write"
        )
        pipeline: ShadowExtractionPipeline | None = request.app.state.extraction_pipeline
        if pipeline is None:
            return ExtractionResult(
                status="failed", reason_codes=("extractor_unconfigured",)
            )
        return pipeline.extract(
            ExtractionSource(
                reference_id=body.reference_id,
                content=body.content,
                sensitivity=body.sensitivity,
            ),
            MemoryScope(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                subject_id=body.subject_id,
                agent_id=body.agent_id,
            ),
        )


__all__ = ["install_extraction_routes"]

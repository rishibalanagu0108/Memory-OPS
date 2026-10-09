"""Authorized lesson promotion, monitoring, and rollback routes."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from pydantic import Field

from memory_ops.agent_learning import (
    LessonControlDecision,
    LessonMonitoringMetrics,
    LessonPromotionEvidence,
    LessonPromotionRegistry,
    LessonScope,
    ToolIdentity,
)
from memory_ops.agent_learning.promotion import LessonPromotionNotFound
from memory_ops.api.schemas import ErrorResponse, WireModel
from memory_ops.api.security import authorize_request
from memory_ops.security import ResourceScope


ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


class LessonScopeBody(WireModel):
    agent_id: UUID
    task_type: Annotated[str, Field(min_length=1, max_length=255)]
    environment: Annotated[str, Field(min_length=1, max_length=255)]
    tool: ToolIdentity


class PromoteLessonBody(WireModel):
    scope: LessonScopeBody
    evidence: LessonPromotionEvidence
    canary_percent: Annotated[int, Field(ge=1, le=100)] = 10


class MonitorLessonBody(WireModel):
    scope: LessonScopeBody
    metrics: LessonMonitoringMetrics


class RollbackLessonBody(WireModel):
    scope: LessonScopeBody
    reason: Annotated[str, Field(min_length=1, max_length=255)]


class LessonControlResponse(WireModel):
    candidate_id: UUID
    control_version: int
    mode: Literal["shadow", "canary", "active", "paused", "rolled_back"]
    evaluated_version_id: UUID
    promoted_version_id: UUID | None
    evidence_id: str | None
    canary_percent: int
    changed: bool
    selectable: bool
    reason_codes: tuple[str, ...]


def _scope(
    tenant_id: UUID, workspace_id: UUID, body: LessonScopeBody
) -> LessonScope:
    return LessonScope(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        **body.model_dump(),
    )


def _response(decision: LessonControlDecision) -> LessonControlResponse:
    state = decision.state
    return LessonControlResponse(
        candidate_id=state.candidate_id,
        control_version=state.control_version,
        mode=state.mode,
        evaluated_version_id=state.evaluated_version_id,
        promoted_version_id=state.promoted_version_id,
        evidence_id=state.evidence_id,
        canary_percent=state.canary_percent,
        changed=decision.changed,
        selectable=decision.selectable,
        reason_codes=decision.reason_codes,
    )


def _registry(request: Request) -> LessonPromotionRegistry:
    return request.app.state.lesson_promotions


def install_agent_learning_routes(app: FastAPI) -> None:
    prefix = "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/agent-lessons"

    @app.post(
        f"{prefix}/{{candidate_id}}/promotions",
        response_model=LessonControlResponse,
        responses=ERROR_RESPONSES,
        tags=["agent-learning"],
    )
    def promote_lesson(
        request: Request,
        tenant_id: UUID,
        workspace_id: UUID,
        candidate_id: UUID,
        body: PromoteLessonBody,
    ) -> LessonControlResponse:
        authorize_request(
            request, ResourceScope(tenant_id, workspace_id), "lesson:promote"
        )
        if body.evidence.candidate_id != candidate_id:
            raise HTTPException(status_code=404)
        try:
            decision = _registry(request).promote(
                _scope(tenant_id, workspace_id, body.scope),
                body.evidence,
                body.canary_percent,
            )
        except LessonPromotionNotFound as error:
            raise HTTPException(status_code=404) from error
        return _response(decision)

    @app.post(
        f"{prefix}/{{candidate_id}}/monitoring",
        response_model=LessonControlResponse,
        responses=ERROR_RESPONSES,
        tags=["agent-learning"],
    )
    def monitor_lesson(
        request: Request,
        tenant_id: UUID,
        workspace_id: UUID,
        candidate_id: UUID,
        body: MonitorLessonBody,
    ) -> LessonControlResponse:
        authorize_request(
            request, ResourceScope(tenant_id, workspace_id), "lesson:monitor"
        )
        try:
            decision = _registry(request).monitor(
                _scope(tenant_id, workspace_id, body.scope),
                candidate_id,
                body.metrics,
            )
        except LessonPromotionNotFound as error:
            raise HTTPException(status_code=404) from error
        return _response(decision)

    @app.post(
        f"{prefix}/{{candidate_id}}/rollback",
        response_model=LessonControlResponse,
        responses=ERROR_RESPONSES,
        tags=["agent-learning"],
    )
    def rollback_lesson(
        request: Request,
        tenant_id: UUID,
        workspace_id: UUID,
        candidate_id: UUID,
        body: RollbackLessonBody,
    ) -> LessonControlResponse:
        authorize_request(
            request, ResourceScope(tenant_id, workspace_id), "lesson:rollback"
        )
        try:
            decision = _registry(request).rollback(
                _scope(tenant_id, workspace_id, body.scope),
                candidate_id,
                body.reason,
            )
        except LessonPromotionNotFound as error:
            raise HTTPException(status_code=404) from error
        return _response(decision)

"""Authorized HTTP routes for explicit user memory."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from pydantic import Field
from sqlalchemy import text

from memory_ops.api.schemas import ErrorResponse, WireModel
from memory_ops.api.security import authorize_request
from memory_ops.lifecycle import LifecycleService, MemoryUnavailable
from memory_ops.persistence import TenantDatabase
from memory_ops.security import ResourceScope
from memory_ops.user_memory import (
    CorrectionRequest,
    EvidenceReference,
    Lifetime,
    MemoryNotFound,
    MemoryScope,
    RememberRequest,
    SemanticType,
    Sensitivity,
    UserMemoryService,
)


ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


class RememberMemoryBody(WireModel):
    subject_id: UUID
    agent_id: UUID | None = None
    semantic_type: SemanticType
    statement: Annotated[str, Field(min_length=1, max_length=10_000)]
    sensitivity: Sensitivity = "normal"
    lifetime: Lifetime = "durable"
    purpose: Annotated[str, Field(min_length=1, max_length=255)]
    access_scope: tuple[Annotated[str, Field(min_length=1, max_length=255)], ...] = ()
    retention_until: datetime | None = None
    valid_from: datetime | None = None
    evidence: tuple[EvidenceReference, ...] = ()


class RememberMemoryResponse(WireModel):
    memory_id: UUID
    version_id: UUID
    operation_id: UUID
    operation_status: Literal["pending", "processing", "completed"]
    replayed: bool


class CorrectMemoryBody(WireModel):
    subject_id: UUID
    agent_id: UUID | None = None
    statement: Annotated[str, Field(min_length=1, max_length=10_000)]
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    evidence: tuple[EvidenceReference, ...] = ()


class CorrectMemoryResponse(RememberMemoryResponse):
    pass


class MemoryResponse(WireModel):
    memory_id: UUID
    version_id: UUID
    version_number: int
    tenant_id: UUID
    workspace_id: UUID
    subject_id: UUID
    agent_id: UUID | None
    semantic_type: SemanticType
    lifecycle: Literal["active", "superseded", "expired", "revoked", "deleted"]
    statement: str
    normalized_subject: str | None
    normalized_predicate: str | None
    normalized_value: object | None
    qualifiers: dict[str, object]
    valid_from: datetime
    valid_to: datetime | None
    recorded_at: datetime
    sensitivity: Sensitivity
    lifetime: Lifetime
    origin: Literal["explicit", "extracted", "derived"]
    purpose: str
    policy_version: str
    access_scope: tuple[str, ...]
    retention_until: datetime | None
    evidence: tuple[EvidenceReference, ...]


class MemoryListResponse(WireModel):
    items: tuple[MemoryResponse, ...]


class OperationStatusResponse(WireModel):
    operation_id: UUID
    status: Literal["pending", "processing", "completed"]
    attempts: int
    last_error_code: str | None


class ForgetMemoryResponse(WireModel):
    memory_id: UUID
    version_id: UUID
    operation_id: UUID
    operation_status: Literal["pending", "processing", "completed"]
    replayed: bool


def _memory_row(connection, memory_id: UUID, workspace_id: UUID):
    return connection.execute(
        text(
            """
            SELECT m.id AS memory_id, v.id AS version_id, v.version_number,
                   m.tenant_id, m.workspace_id, m.subject_id, m.agent_id,
                   m.semantic_type, m.lifecycle, v.original_statement AS statement,
                   v.normalized_subject, v.normalized_predicate, v.normalized_value,
                   v.qualifiers, v.valid_from, v.valid_to, v.recorded_at,
                   v.sensitivity, v.lifetime, v.origin, v.purpose,
                   v.policy_version, v.access_scope, v.retention_until,
                   COALESCE(
                       (SELECT jsonb_agg(jsonb_build_object(
                           'evidence_type', e.evidence_type,
                           'reference_id', e.reference_id,
                           'locator', e.locator
                       ) ORDER BY e.created_at, e.id)
                        FROM user_memory_evidence e
                        WHERE e.memory_version_id = v.id),
                       '[]'::jsonb
                   ) AS evidence
            FROM user_memories m
            JOIN user_memory_versions v ON v.id = m.current_version_id
            WHERE m.id = :memory_id
              AND m.workspace_id = :workspace_id
              AND m.lifecycle = 'active'
              AND (v.retention_until IS NULL OR v.retention_until > now())
            """
        ),
        {"memory_id": memory_id, "workspace_id": workspace_id},
    ).mappings().one_or_none()


def _response(row) -> MemoryResponse:
    return MemoryResponse.model_validate(dict(row))


def install_user_memory_routes(app: FastAPI) -> None:
    prefix = "/v1/tenants/{tenant_id}/workspaces/{workspace_id}"

    @app.post(
        f"{prefix}/memories",
        response_model=RememberMemoryResponse,
        status_code=201,
        responses=ERROR_RESPONSES,
        tags=["user-memory"],
    )
    def remember_memory(
        request: Request,
        response: Response,
        tenant_id: UUID,
        workspace_id: UUID,
        body: RememberMemoryBody,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> RememberMemoryResponse:
        authorize_request(
            request, ResourceScope(tenant_id, workspace_id), "memory:write"
        )
        database: TenantDatabase = request.app.state.database
        boundary = request.app.state.security
        policy_version = boundary.policy.version if boundary.policy else "unconfigured"
        result = UserMemoryService(database, policy_version).remember(
            RememberRequest(
                scope=MemoryScope(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    subject_id=body.subject_id,
                    agent_id=body.agent_id,
                ),
                **body.model_dump(exclude={"subject_id", "agent_id"}),
            ),
            idempotency_key,
        )
        with database.transaction(tenant_id) as connection:
            operation = connection.execute(
                text(
                    """
                    SELECT id, status
                    FROM outbox_events
                    WHERE resource_type = 'user_memory'
                      AND resource_id = :memory_id
                      AND resource_version = :version_id
                    """
                ),
                {
                    "memory_id": result.receipt.resource_id,
                    "version_id": result.receipt.resource_version,
                },
            ).one()
        response.status_code = 200 if result.replayed else 201
        return RememberMemoryResponse(
            memory_id=result.receipt.resource_id,
            version_id=result.receipt.resource_version,
            operation_id=operation.id,
            operation_status=operation.status,
            replayed=result.replayed,
        )

    @app.post(
        f"{prefix}/memories/{{memory_id}}/corrections",
        response_model=CorrectMemoryResponse,
        status_code=201,
        responses=ERROR_RESPONSES,
        tags=["user-memory"],
    )
    def correct_memory(
        request: Request,
        response: Response,
        tenant_id: UUID,
        workspace_id: UUID,
        memory_id: UUID,
        body: CorrectMemoryBody,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> CorrectMemoryResponse:
        authorize_request(
            request, ResourceScope(tenant_id, workspace_id), "memory:write"
        )
        database: TenantDatabase = request.app.state.database
        boundary = request.app.state.security
        policy_version = boundary.policy.version if boundary.policy else "unconfigured"
        try:
            result = UserMemoryService(database, policy_version).correct(
                CorrectionRequest(
                    scope=MemoryScope(
                        tenant_id=tenant_id,
                        workspace_id=workspace_id,
                        subject_id=body.subject_id,
                        agent_id=body.agent_id,
                    ),
                    memory_id=memory_id,
                    **body.model_dump(exclude={"subject_id", "agent_id"}),
                ),
                idempotency_key,
            )
        except MemoryNotFound as error:
            raise HTTPException(status_code=404) from error
        with database.transaction(tenant_id) as connection:
            operation = connection.execute(
                text(
                    """
                    SELECT id, status
                    FROM outbox_events
                    WHERE event_type = 'user_memory.version.corrected'
                      AND resource_id = :memory_id
                      AND resource_version = :version_id
                    """
                ),
                {
                    "memory_id": result.receipt.resource_id,
                    "version_id": result.receipt.resource_version,
                },
            ).one()
        response.status_code = 200 if result.replayed else 201
        return CorrectMemoryResponse(
            memory_id=result.receipt.resource_id,
            version_id=result.receipt.resource_version,
            operation_id=operation.id,
            operation_status=operation.status,
            replayed=result.replayed,
        )

    @app.get(
        f"{prefix}/memories/{{memory_id}}",
        response_model=MemoryResponse,
        responses=ERROR_RESPONSES,
        tags=["user-memory"],
    )
    def inspect_memory(
        request: Request,
        tenant_id: UUID,
        workspace_id: UUID,
        memory_id: UUID,
    ) -> MemoryResponse:
        authorize_request(
            request, ResourceScope(tenant_id, workspace_id), "memory:read"
        )
        database: TenantDatabase = request.app.state.database
        with database.transaction(tenant_id) as connection:
            row = _memory_row(connection, memory_id, workspace_id)
        if row is None:
            raise HTTPException(status_code=404)
        return _response(row)

    @app.delete(
        f"{prefix}/memories/{{memory_id}}",
        response_model=ForgetMemoryResponse,
        status_code=202,
        responses=ERROR_RESPONSES,
        tags=["user-memory"],
    )
    def forget_memory(
        request: Request,
        tenant_id: UUID,
        workspace_id: UUID,
        memory_id: UUID,
        subject_id: UUID,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        agent_id: UUID | None = None,
    ) -> ForgetMemoryResponse:
        authorize_request(
            request, ResourceScope(tenant_id, workspace_id), "memory:write"
        )
        database: TenantDatabase = request.app.state.database
        try:
            result = LifecycleService(database).forget(
                MemoryScope(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    subject_id=subject_id,
                    agent_id=agent_id,
                ),
                memory_id,
                idempotency_key,
            )
        except MemoryUnavailable as error:
            raise HTTPException(status_code=404) from error
        with database.transaction(tenant_id) as connection:
            operation = connection.execute(
                text(
                    """
                    SELECT id, status
                    FROM outbox_events
                    WHERE event_type = 'user_memory.purge.requested'
                      AND resource_id = :memory_id
                      AND resource_version = :version_id
                    """
                ),
                {
                    "memory_id": result.receipt.resource_id,
                    "version_id": result.receipt.resource_version,
                },
            ).one()
        return ForgetMemoryResponse(
            memory_id=result.receipt.resource_id,
            version_id=result.receipt.resource_version,
            operation_id=operation.id,
            operation_status=operation.status,
            replayed=result.replayed,
        )

    @app.get(
        f"{prefix}/memories",
        response_model=MemoryListResponse,
        responses=ERROR_RESPONSES,
        tags=["user-memory"],
    )
    def list_memories(
        request: Request,
        tenant_id: UUID,
        workspace_id: UUID,
        subject_id: UUID | None = None,
        purpose: Annotated[str | None, Query(max_length=255)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> MemoryListResponse:
        authorize_request(
            request, ResourceScope(tenant_id, workspace_id), "memory:read"
        )
        database: TenantDatabase = request.app.state.database
        with database.transaction(tenant_id) as connection:
            ids = connection.execute(
                text(
                    """
                    SELECT m.id
                    FROM user_memories m
                    JOIN user_memory_versions v ON v.id = m.current_version_id
                    WHERE m.workspace_id = :workspace_id
                      AND m.lifecycle = 'active'
                      AND (v.retention_until IS NULL OR v.retention_until > now())
                      AND (
                          CAST(:subject_id AS uuid) IS NULL
                          OR m.subject_id = CAST(:subject_id AS uuid)
                      )
                      AND (
                          CAST(:purpose AS varchar) IS NULL
                          OR v.purpose = CAST(:purpose AS varchar)
                      )
                    ORDER BY m.created_at, m.id
                    LIMIT :limit
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "subject_id": subject_id,
                    "purpose": purpose,
                    "limit": limit,
                },
            ).scalars().all()
            rows = [_memory_row(connection, memory_id, workspace_id) for memory_id in ids]
        return MemoryListResponse(items=tuple(_response(row) for row in rows))

    @app.get(
        f"{prefix}/operations/{{operation_id}}",
        response_model=OperationStatusResponse,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    def operation_status(
        request: Request,
        tenant_id: UUID,
        workspace_id: UUID,
        operation_id: UUID,
    ) -> OperationStatusResponse:
        authorize_request(
            request, ResourceScope(tenant_id, workspace_id), "memory:read"
        )
        database: TenantDatabase = request.app.state.database
        with database.transaction(tenant_id) as connection:
            row = connection.execute(
                text(
                    """
                    SELECT o.id AS operation_id, o.status, o.attempts,
                           o.last_error_code
                    FROM outbox_events o
                    JOIN user_memories m ON m.id = o.resource_id
                    WHERE o.id = :operation_id
                      AND o.resource_type = 'user_memory'
                      AND m.workspace_id = :workspace_id
                    """
                ),
                {"operation_id": operation_id, "workspace_id": workspace_id},
            ).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404)
        return OperationStatusResponse.model_validate(dict(row))

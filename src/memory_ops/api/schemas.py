"""Shared wire schemas for API identity and errors."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TenantIdentity(WireModel):
    tenant_id: UUID


class WorkspaceIdentity(WireModel):
    tenant_id: UUID
    workspace_id: UUID


class SharedSpaceIdentity(WireModel):
    tenant_id: UUID
    shared_space_id: UUID


class PrincipalIdentity(WireModel):
    tenant_id: UUID
    principal_id: UUID


class AgentIdentity(WireModel):
    tenant_id: UUID
    agent_id: UUID


class SubjectIdentity(WireModel):
    tenant_id: UUID
    subject_id: UUID


class SessionIdentity(WireModel):
    tenant_id: UUID
    session_id: UUID


class ResourceIdentity(WireModel):
    tenant_id: UUID
    resource_id: UUID
    resource_type: str


class ErrorResponse(WireModel):
    code: Literal[
        "invalid_request",
        "unauthenticated",
        "permission_denied",
        "not_found",
        "conflict",
        "internal_error",
    ]
    message: str
    request_id: UUID | None = None


SHARED_SCHEMAS = (
    TenantIdentity,
    WorkspaceIdentity,
    SharedSpaceIdentity,
    PrincipalIdentity,
    AgentIdentity,
    SubjectIdentity,
    SessionIdentity,
    ResourceIdentity,
    ErrorResponse,
)


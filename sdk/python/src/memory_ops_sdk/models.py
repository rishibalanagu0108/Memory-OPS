"""Typed Memory-ops API models."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


SemanticType = Literal["fact", "preference", "goal", "constraint", "episode"]
Sensitivity = Literal["normal", "sensitive", "restricted"]
Lifetime = Literal["session", "temporary", "durable"]
OperationState = Literal["pending", "processing", "completed"]
EvidenceType = Literal[
    "conversation_message", "document", "event", "external_record"
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceReference(Model):
    evidence_type: EvidenceType
    reference_id: str
    locator: str | None = None


class RememberMemoryRequest(Model):
    subject_id: UUID
    agent_id: UUID | None = None
    semantic_type: SemanticType
    statement: str = Field(min_length=1, max_length=10_000)
    sensitivity: Sensitivity = "normal"
    lifetime: Lifetime = "durable"
    purpose: str = Field(min_length=1, max_length=255)
    access_scope: tuple[str, ...] = ()
    retention_until: datetime | None = None
    valid_from: datetime | None = None
    evidence: tuple[EvidenceReference, ...] = ()


class RememberMemoryResult(Model):
    memory_id: UUID
    version_id: UUID
    operation_id: UUID
    operation_status: OperationState
    replayed: bool


class Memory(Model):
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


class MemoryList(Model):
    items: tuple[Memory, ...]


class OperationStatus(Model):
    operation_id: UUID
    status: OperationState
    attempts: int
    last_error_code: str | None

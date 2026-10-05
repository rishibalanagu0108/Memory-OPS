"""Canonical user-memory domain values shared by persistence and APIs."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


SemanticType = Literal["fact", "preference", "goal", "constraint", "episode"]
Sensitivity = Literal["normal", "sensitive", "restricted"]
Lifetime = Literal["session", "temporary", "durable"]
Origin = Literal["explicit", "extracted", "derived"]
Lifecycle = Literal["active", "superseded", "expired", "revoked", "deleted"]
ChangeKind = Literal["initial", "correction", "temporal_change"]
BoundedText = Annotated[str, Field(min_length=1, max_length=255)]


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryScope(DomainModel):
    tenant_id: UUID
    workspace_id: UUID
    subject_id: UUID
    agent_id: UUID | None = None


class GovernanceMetadata(DomainModel):
    sensitivity: Sensitivity
    lifetime: Lifetime
    origin: Origin
    purpose: BoundedText
    policy_version: BoundedText
    access_scope: tuple[BoundedText, ...] = ()
    retention_until: datetime | None = None


class EvidenceReference(DomainModel):
    evidence_type: Literal[
        "conversation_message", "document", "event", "external_record"
    ]
    reference_id: BoundedText
    locator: BoundedText | None = None


class CanonicalMemoryVersion(DomainModel):
    id: UUID
    memory_id: UUID
    version_number: Annotated[int, Field(ge=1)]
    scope: MemoryScope
    semantic_type: SemanticType
    original_statement: Annotated[str, Field(min_length=1)]
    normalized_subject: BoundedText | None = None
    normalized_predicate: BoundedText | None = None
    normalized_value: object | None = None
    qualifiers: dict[str, object] = Field(default_factory=dict)
    valid_from: datetime
    valid_to: datetime | None = None
    recorded_at: datetime
    governance: GovernanceMetadata
    lifecycle: Lifecycle = "active"
    evidence: tuple[EvidenceReference, ...] = ()
    supersedes_version_id: UUID | None = None
    change_kind: ChangeKind = "initial"

    @model_validator(mode="after")
    def valid_time_is_ordered(self) -> "CanonicalMemoryVersion":
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class LogicalMemory(DomainModel):
    id: UUID
    scope: MemoryScope
    semantic_type: SemanticType
    lifecycle: Lifecycle = "active"
    current_version_id: UUID | None = None


class DerivedArtifactIdentity(DomainModel):
    """Identity required on every rebuildable index or projection artifact."""

    memory_id: UUID
    canonical_version_id: UUID
    index_generation: BoundedText
    model_version: BoundedText


from memory_ops.user_memory.service import (  # noqa: E402
    CorrectionRequest,
    MemoryNotFound,
    RememberRequest,
    UserMemoryService,
)
from memory_ops.user_memory.decisions import (  # noqa: E402
    AdmissionCandidate,
    AdmissionDecision,
    AdmissionKind,
    decide_admission,
)

__all__ = [
    "AdmissionCandidate",
    "AdmissionDecision",
    "AdmissionKind",
    "CanonicalMemoryVersion",
    "CorrectionRequest",
    "DerivedArtifactIdentity",
    "EvidenceReference",
    "GovernanceMetadata",
    "LogicalMemory",
    "MemoryScope",
    "MemoryNotFound",
    "RememberRequest",
    "UserMemoryService",
    "decide_admission",
]

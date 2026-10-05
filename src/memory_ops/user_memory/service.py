"""Deterministic admission and atomic persistence for explicit memories."""

import json
from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection

from memory_ops.persistence import TenantDatabase
from memory_ops.persistence.writes import (
    IdempotentWriter,
    OutboxMessage,
    WriteReceipt,
    WriteResult,
)
from memory_ops.security import enforce_content_admission
from memory_ops.user_memory import (
    DomainModel,
    EvidenceReference,
    GovernanceMetadata,
    Lifetime,
    MemoryScope,
    CanonicalMemoryVersion,
    SemanticType,
    Sensitivity,
)


class MemoryNotFound(Exception):
    """The requested current memory does not exist in the supplied scope."""


class RememberRequest(DomainModel):
    scope: MemoryScope
    semantic_type: SemanticType
    statement: Annotated[str, Field(min_length=1, max_length=10_000)]
    sensitivity: Sensitivity = "normal"
    lifetime: Lifetime = "durable"
    purpose: Annotated[str, Field(min_length=1, max_length=255)]
    access_scope: tuple[Annotated[str, Field(min_length=1, max_length=255)], ...] = ()
    retention_until: datetime | None = None
    valid_from: datetime | None = None
    evidence: tuple[EvidenceReference, ...] = ()

    @field_validator("statement")
    @classmethod
    def statement_has_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("statement must contain non-whitespace content")
        return value


class CorrectionRequest(DomainModel):
    scope: MemoryScope
    memory_id: UUID
    statement: Annotated[str, Field(min_length=1, max_length=10_000)]
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    evidence: tuple[EvidenceReference, ...] = ()

    @field_validator("statement")
    @classmethod
    def statement_has_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("statement must contain non-whitespace content")
        return value

    @model_validator(mode="after")
    def valid_time_is_ordered(self) -> "CorrectionRequest":
        if self.valid_from is not None and self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class UserMemoryService:
    def __init__(self, database: TenantDatabase, policy_version: str) -> None:
        if not policy_version or len(policy_version) > 255:
            raise ValueError("policy version must contain 1 to 255 characters")
        self.writer = IdempotentWriter(database)
        self.policy_version = policy_version

    def remember(self, request: RememberRequest, idempotency_key: str) -> WriteResult:
        enforce_content_admission(request.statement)
        request_body = json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return self.writer.execute(
            request.scope.tenant_id,
            idempotency_key,
            "user_memory.remember",
            request_body,
            lambda connection: self._persist(connection, request),
        )

    def correct(self, request: CorrectionRequest, idempotency_key: str) -> WriteResult:
        enforce_content_admission(request.statement)
        request_body = json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return self.writer.execute(
            request.scope.tenant_id,
            idempotency_key,
            "user_memory.correct",
            request_body,
            lambda connection: self._persist_correction(connection, request),
        )

    def current_version(
        self, scope: MemoryScope, memory_id: UUID
    ) -> CanonicalMemoryVersion:
        with self.writer.database.transaction(scope.tenant_id) as connection:
            row = connection.execute(
                text(
                    f"""
                    {self._version_select()}
                    WHERE m.id = :memory_id
                      AND m.workspace_id = :workspace_id
                      AND m.subject_id = :subject_id
                      AND m.agent_id IS NOT DISTINCT FROM :agent_id
                      AND m.lifecycle = 'active'
                      AND v.id = m.current_version_id
                    """
                ),
                {
                    "memory_id": memory_id,
                    "workspace_id": scope.workspace_id,
                    "subject_id": scope.subject_id,
                    "agent_id": scope.agent_id,
                },
            ).mappings().one_or_none()
            if row is None:
                raise MemoryNotFound(memory_id)
            return self._to_version(connection, row, "active")

    def version_at(
        self,
        scope: MemoryScope,
        memory_id: UUID,
        valid_at: datetime,
        recorded_at: datetime | None = None,
    ) -> CanonicalMemoryVersion:
        with self.writer.database.transaction(scope.tenant_id) as connection:
            row = connection.execute(
                text(
                    f"""
                    {self._version_select()}
                    WHERE m.id = :memory_id
                      AND m.workspace_id = :workspace_id
                      AND m.subject_id = :subject_id
                      AND m.agent_id IS NOT DISTINCT FROM :agent_id
                      AND v.valid_from <= :valid_at
                      AND (v.valid_to IS NULL OR v.valid_to > :valid_at)
                      AND (
                          CAST(:recorded_at AS timestamptz) IS NULL
                          OR v.recorded_at <= :recorded_at
                      )
                    ORDER BY v.valid_from DESC, v.recorded_at DESC, v.version_number DESC
                    LIMIT 1
                    """
                ),
                {
                    "memory_id": memory_id,
                    "workspace_id": scope.workspace_id,
                    "subject_id": scope.subject_id,
                    "agent_id": scope.agent_id,
                    "valid_at": valid_at,
                    "recorded_at": recorded_at,
                },
            ).mappings().one_or_none()
            if row is None:
                raise MemoryNotFound(memory_id)
            lifecycle = "active" if row.id == row.current_version_id else "superseded"
            return self._to_version(connection, row, lifecycle)

    def _persist(
        self, connection: Connection, request: RememberRequest
    ) -> tuple[WriteReceipt, tuple[OutboxMessage, ...]]:
        memory_id, version_id = uuid4(), uuid4()
        scope = request.scope
        normalized_statement = " ".join(request.statement.split())
        connection.execute(
            text(
                """
                INSERT INTO user_memories (
                    id, tenant_id, workspace_id, subject_id, agent_id, semantic_type
                ) VALUES (
                    :id, :tenant_id, :workspace_id, :subject_id, :agent_id, :semantic_type
                )
                """
            ),
            {
                "id": memory_id,
                "tenant_id": scope.tenant_id,
                "workspace_id": scope.workspace_id,
                "subject_id": scope.subject_id,
                "agent_id": scope.agent_id,
                "semantic_type": request.semantic_type,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO user_memory_versions (
                    id, tenant_id, memory_id, version_number, original_statement,
                    normalized_subject, normalized_predicate, normalized_value,
                    valid_from, sensitivity, lifetime, origin, purpose,
                    policy_version, access_scope, retention_until
                ) VALUES (
                    :id, :tenant_id, :memory_id, 1, :original_statement,
                    :normalized_subject, :normalized_predicate,
                    CAST(:normalized_value AS jsonb), COALESCE(:valid_from, now()),
                    :sensitivity, :lifetime, 'explicit', :purpose,
                    :policy_version, CAST(:access_scope AS jsonb), :retention_until
                )
                """
            ),
            {
                "id": version_id,
                "tenant_id": scope.tenant_id,
                "memory_id": memory_id,
                "original_statement": request.statement,
                "normalized_subject": str(scope.subject_id),
                "normalized_predicate": request.semantic_type,
                "normalized_value": json.dumps(normalized_statement),
                "valid_from": request.valid_from,
                "sensitivity": request.sensitivity,
                "lifetime": request.lifetime,
                "purpose": request.purpose,
                "policy_version": self.policy_version,
                "access_scope": json.dumps(request.access_scope),
                "retention_until": request.retention_until,
            },
        )
        for evidence in request.evidence:
            connection.execute(
                text(
                    """
                    INSERT INTO user_memory_evidence (
                        id, tenant_id, memory_version_id, evidence_type,
                        reference_id, locator
                    ) VALUES (
                        :id, :tenant_id, :version_id, :evidence_type,
                        :reference_id, :locator
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "tenant_id": scope.tenant_id,
                    "version_id": version_id,
                    "evidence_type": evidence.evidence_type,
                    "reference_id": evidence.reference_id,
                    "locator": evidence.locator,
                },
            )
        connection.execute(
            text(
                "UPDATE user_memories SET current_version_id = :version_id "
                "WHERE id = :memory_id"
            ),
            {"version_id": version_id, "memory_id": memory_id},
        )
        receipt = WriteReceipt("user_memory", memory_id, version_id)
        event = OutboxMessage(
            "user_memory.version.created", "user_memory", memory_id, version_id
        )
        return receipt, (event,)

    def _persist_correction(
        self, connection: Connection, request: CorrectionRequest
    ) -> tuple[WriteReceipt, tuple[OutboxMessage, ...]]:
        scope = request.scope
        current = connection.execute(
            text(
                """
                SELECT v.*
                FROM user_memories m
                JOIN user_memory_versions v ON v.id = m.current_version_id
                WHERE m.id = :memory_id
                  AND m.workspace_id = :workspace_id
                  AND m.subject_id = :subject_id
                  AND m.agent_id IS NOT DISTINCT FROM :agent_id
                  AND m.lifecycle = 'active'
                FOR UPDATE OF m
                """
            ),
            {
                "memory_id": request.memory_id,
                "workspace_id": scope.workspace_id,
                "subject_id": scope.subject_id,
                "agent_id": scope.agent_id,
            },
        ).mappings().one_or_none()
        if current is None:
            raise MemoryNotFound(request.memory_id)

        version_id = uuid4()
        normalized_statement = " ".join(request.statement.split())
        connection.execute(
            text(
                """
                INSERT INTO user_memory_versions (
                    id, tenant_id, memory_id, version_number, original_statement,
                    normalized_subject, normalized_predicate, normalized_value,
                    qualifiers, valid_from, valid_to, sensitivity, lifetime,
                    origin, purpose, policy_version, access_scope, retention_until,
                    supersedes_version_id, change_kind
                ) VALUES (
                    :id, :tenant_id, :memory_id, :version_number,
                    :original_statement, :normalized_subject,
                    :normalized_predicate, CAST(:normalized_value AS jsonb),
                    CAST(:qualifiers AS jsonb), :valid_from, :valid_to,
                    :sensitivity, :lifetime, 'explicit', :purpose,
                    :policy_version, CAST(:access_scope AS jsonb), :retention_until,
                    :supersedes_version_id, 'correction'
                )
                """
            ),
            {
                "id": version_id,
                "tenant_id": scope.tenant_id,
                "memory_id": request.memory_id,
                "version_number": current.version_number + 1,
                "original_statement": request.statement,
                "normalized_subject": current.normalized_subject,
                "normalized_predicate": current.normalized_predicate,
                "normalized_value": json.dumps(normalized_statement),
                "qualifiers": json.dumps(current.qualifiers),
                "valid_from": request.valid_from or current.valid_from,
                "valid_to": request.valid_to if request.valid_to is not None else current.valid_to,
                "sensitivity": current.sensitivity,
                "lifetime": current.lifetime,
                "purpose": current.purpose,
                "policy_version": self.policy_version,
                "access_scope": json.dumps(current.access_scope),
                "retention_until": current.retention_until,
                "supersedes_version_id": current.id,
            },
        )
        for evidence in request.evidence:
            connection.execute(
                text(
                    """
                    INSERT INTO user_memory_evidence (
                        id, tenant_id, memory_version_id, evidence_type,
                        reference_id, locator
                    ) VALUES (
                        :id, :tenant_id, :version_id, :evidence_type,
                        :reference_id, :locator
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "tenant_id": scope.tenant_id,
                    "version_id": version_id,
                    "evidence_type": evidence.evidence_type,
                    "reference_id": evidence.reference_id,
                    "locator": evidence.locator,
                },
            )
        updated = connection.execute(
            text(
                """
                UPDATE user_memories
                SET current_version_id = :version_id
                WHERE id = :memory_id AND current_version_id = :previous_version_id
                """
            ),
            {
                "version_id": version_id,
                "memory_id": request.memory_id,
                "previous_version_id": current.id,
            },
        )
        if updated.rowcount != 1:
            raise RuntimeError("current memory version changed during correction")
        return (
            WriteReceipt("user_memory", request.memory_id, version_id),
            (
                OutboxMessage(
                    "user_memory.version.corrected",
                    "user_memory",
                    request.memory_id,
                    version_id,
                ),
            ),
        )

    @staticmethod
    def _version_select() -> str:
        return """
            SELECT v.*, m.workspace_id, m.subject_id, m.agent_id,
                   m.semantic_type, m.current_version_id
            FROM user_memory_versions v
            JOIN user_memories m ON m.id = v.memory_id
        """

    @staticmethod
    def _to_version(
        connection: Connection, row, lifecycle: str
    ) -> CanonicalMemoryVersion:
        evidence = connection.execute(
            text(
                """
                SELECT evidence_type, reference_id, locator
                FROM user_memory_evidence
                WHERE memory_version_id = :version_id
                ORDER BY created_at, id
                """
            ),
            {"version_id": row.id},
        ).mappings()
        return CanonicalMemoryVersion(
            id=row.id,
            memory_id=row.memory_id,
            version_number=row.version_number,
            scope=MemoryScope(
                tenant_id=row.tenant_id,
                workspace_id=row.workspace_id,
                subject_id=row.subject_id,
                agent_id=row.agent_id,
            ),
            semantic_type=row.semantic_type,
            original_statement=row.original_statement,
            normalized_subject=row.normalized_subject,
            normalized_predicate=row.normalized_predicate,
            normalized_value=row.normalized_value,
            qualifiers=row.qualifiers,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            recorded_at=row.recorded_at,
            governance=GovernanceMetadata(
                sensitivity=row.sensitivity,
                lifetime=row.lifetime,
                origin=row.origin,
                purpose=row.purpose,
                policy_version=row.policy_version,
                access_scope=tuple(row.access_scope),
                retention_until=row.retention_until,
            ),
            lifecycle=lifecycle,
            evidence=tuple(EvidenceReference.model_validate(item) for item in evidence),
            supersedes_version_id=row.supersedes_version_id,
            change_kind=row.change_kind,
        )

"""Deterministic admission and atomic persistence for explicit memories."""

import json
from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import Field, field_validator
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
    Lifetime,
    MemoryScope,
    SemanticType,
    Sensitivity,
)


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

"""Synchronous memory revocation and versioned purge scheduling."""

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memory_ops.persistence import TenantDatabase
from memory_ops.persistence.writes import (
    IdempotentWriter,
    OutboxMessage,
    WriteReceipt,
    WriteResult,
)
from memory_ops.user_memory import MemoryScope


class MemoryUnavailable(Exception):
    """The memory is absent or no longer active in the supplied scope."""


@dataclass(frozen=True)
class ExpirationResult:
    memory_id: UUID
    version_id: UUID
    purge_event_id: UUID


class LifecycleService:
    def __init__(self, database: TenantDatabase) -> None:
        self.database = database
        self.writer = IdempotentWriter(database)

    def forget(
        self, scope: MemoryScope, memory_id: UUID, idempotency_key: str
    ) -> WriteResult:
        request_body = json.dumps(
            {"memory_id": str(memory_id), "scope": scope.model_dump(mode="json")},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return self.writer.execute(
            scope.tenant_id,
            idempotency_key,
            "user_memory.forget",
            request_body,
            lambda connection: self._revoke(connection, scope, memory_id),
        )

    def expire_due(
        self, tenant_id: UUID, *, at: datetime | None = None, limit: int = 100
    ) -> tuple[ExpirationResult, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.database.transaction(tenant_id) as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT m.id, m.current_version_id
                    FROM user_memories m
                    JOIN user_memory_versions v ON v.id = m.current_version_id
                    WHERE m.lifecycle = 'active'
                      AND v.retention_until IS NOT NULL
                      AND v.retention_until <= COALESCE(:at, now())
                    ORDER BY v.retention_until, m.id
                    FOR UPDATE OF m SKIP LOCKED
                    LIMIT :limit
                    """
                ),
                {"at": at, "limit": limit},
            ).all()
            results = []
            for memory_id, version_id in rows:
                event_id = uuid4()
                connection.execute(
                    text(
                        "UPDATE user_memories SET lifecycle = 'expired' "
                        "WHERE id = :memory_id AND lifecycle = 'active'"
                    ),
                    {"memory_id": memory_id},
                )
                self._enqueue_purge(
                    connection,
                    tenant_id,
                    memory_id,
                    version_id,
                    event_id,
                )
                results.append(ExpirationResult(memory_id, version_id, event_id))
            return tuple(results)

    def is_retrievable(
        self, tenant_id: UUID, memory_id: UUID, version_id: UUID
    ) -> bool:
        with self.database.transaction(tenant_id) as connection:
            return bool(
                connection.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM user_memories m
                            JOIN user_memory_versions v ON v.id = m.current_version_id
                            WHERE m.id = :memory_id
                              AND m.current_version_id = :version_id
                              AND m.lifecycle = 'active'
                              AND (
                                  v.retention_until IS NULL
                                  OR v.retention_until > now()
                              )
                        )
                        """
                    ),
                    {"memory_id": memory_id, "version_id": version_id},
                ).scalar_one()
            )

    def _revoke(
        self, connection: Connection, scope: MemoryScope, memory_id: UUID
    ) -> tuple[WriteReceipt, tuple[OutboxMessage, ...]]:
        row = connection.execute(
            text(
                """
                UPDATE user_memories
                SET lifecycle = 'revoked'
                WHERE id = :memory_id
                  AND workspace_id = :workspace_id
                  AND subject_id = :subject_id
                  AND agent_id IS NOT DISTINCT FROM :agent_id
                  AND lifecycle = 'active'
                RETURNING current_version_id
                """
            ),
            {
                "memory_id": memory_id,
                "workspace_id": scope.workspace_id,
                "subject_id": scope.subject_id,
                "agent_id": scope.agent_id,
            },
        ).one_or_none()
        if row is None:
            raise MemoryUnavailable(memory_id)
        version_id = row.current_version_id
        return (
            WriteReceipt("user_memory", memory_id, version_id),
            (
                OutboxMessage(
                    "user_memory.purge.requested",
                    "user_memory",
                    memory_id,
                    version_id,
                ),
            ),
        )

    @staticmethod
    def _enqueue_purge(
        connection: Connection,
        tenant_id: UUID,
        memory_id: UUID,
        version_id: UUID,
        event_id: UUID,
    ) -> None:
        connection.execute(
            text(
                """
                INSERT INTO outbox_events (
                    id, tenant_id, event_type, resource_type,
                    resource_id, resource_version
                ) VALUES (
                    :id, :tenant_id, 'user_memory.purge.requested',
                    'user_memory', :memory_id, :version_id
                )
                """
            ),
            {
                "id": event_id,
                "tenant_id": tenant_id,
                "memory_id": memory_id,
                "version_id": version_id,
            },
        )

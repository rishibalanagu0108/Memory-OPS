"""Atomic idempotent writes backed by the transactional outbox."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memory_ops.persistence import TenantDatabase


class IdempotencyConflict(Exception):
    """An idempotency key was reused for a different request."""


@dataclass(frozen=True)
class WriteReceipt:
    resource_type: str
    resource_id: UUID
    resource_version: UUID | None = None


@dataclass(frozen=True)
class OutboxMessage:
    event_type: str
    resource_type: str
    resource_id: UUID
    resource_version: UUID | None = None
    event_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True)
class WriteResult:
    receipt: WriteReceipt
    replayed: bool


Mutation = Callable[
    [Connection], tuple[WriteReceipt, Iterable[OutboxMessage]]
]


class IdempotentWriter:
    def __init__(self, database: TenantDatabase) -> None:
        self.database = database

    def execute(
        self,
        tenant_id: UUID,
        idempotency_key: str,
        operation: str,
        request_body: bytes,
        mutation: Mutation,
    ) -> WriteResult:
        if not idempotency_key or len(idempotency_key) > 255:
            raise ValueError("idempotency key must contain 1 to 255 characters")
        if not operation or len(operation) > 100:
            raise ValueError("operation must contain 1 to 100 characters")

        request_hash = sha256(operation.encode() + b"\0" + request_body).hexdigest()
        with self.database.transaction(tenant_id) as connection:
            inserted = connection.execute(
                text(
                    """
                    INSERT INTO idempotency_records (
                        tenant_id, idempotency_key, operation, request_hash
                    ) VALUES (:tenant_id, :key, :operation, :request_hash)
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    RETURNING idempotency_key
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "key": idempotency_key,
                    "operation": operation,
                    "request_hash": request_hash,
                },
            ).scalar_one_or_none()
            if inserted is None:
                return self._replay(connection, idempotency_key, request_hash)

            receipt, messages = mutation(connection)
            messages = tuple(messages)
            if not messages:
                raise ValueError("an acknowledged write requires an outbox event")
            for message in messages:
                connection.execute(
                    text(
                        """
                        INSERT INTO outbox_events (
                            id, tenant_id, event_type, resource_type,
                            resource_id, resource_version
                        ) VALUES (
                            :id, :tenant_id, :event_type, :resource_type,
                            :resource_id, :resource_version
                        )
                        """
                    ),
                    {
                        "id": message.event_id,
                        "tenant_id": tenant_id,
                        "event_type": message.event_type,
                        "resource_type": message.resource_type,
                        "resource_id": message.resource_id,
                        "resource_version": message.resource_version,
                    },
                )
            connection.execute(
                text(
                    """
                    UPDATE idempotency_records
                    SET resource_type = :resource_type,
                        resource_id = :resource_id,
                        resource_version = :resource_version,
                        completed_at = now()
                    WHERE tenant_id = :tenant_id AND idempotency_key = :key
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "key": idempotency_key,
                    "resource_type": receipt.resource_type,
                    "resource_id": receipt.resource_id,
                    "resource_version": receipt.resource_version,
                },
            )
            return WriteResult(receipt, replayed=False)

    @staticmethod
    def _replay(
        connection: Connection,
        idempotency_key: str,
        request_hash: str,
    ) -> WriteResult:
        record = connection.execute(
            text(
                """
                SELECT request_hash, resource_type, resource_id, resource_version
                FROM idempotency_records
                WHERE idempotency_key = :key
                """
            ),
            {"key": idempotency_key},
        ).one()
        if record.request_hash != request_hash:
            raise IdempotencyConflict("idempotency key belongs to another request")
        return WriteResult(
            WriteReceipt(
                resource_type=record.resource_type,
                resource_id=record.resource_id,
                resource_version=record.resource_version,
            ),
            replayed=True,
        )

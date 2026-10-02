"""Transactional outbox claiming and status operations."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from memory_ops.persistence import TenantDatabase


@dataclass(frozen=True)
class OutboxEvent:
    id: UUID
    event_type: str
    resource_type: str
    resource_id: UUID
    resource_version: UUID | None
    attempts: int


@dataclass(frozen=True)
class OutboxStatus:
    status: str
    attempts: int
    last_error_code: str | None


class OutboxWorker:
    def __init__(self, database: TenantDatabase, lease_seconds: int = 60) -> None:
        if lease_seconds < 0:
            raise ValueError("lease_seconds cannot be negative")
        self.database = database
        self.lease_seconds = lease_seconds

    def claim(self, tenant_id: UUID, limit: int = 100) -> list[OutboxEvent]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.database.transaction(tenant_id) as connection:
            rows = connection.execute(
                text(
                    """
                    WITH claimable AS (
                        SELECT id
                        FROM outbox_events
                        WHERE available_at <= now()
                          AND (
                            status = 'pending'
                            OR (
                                status = 'processing'
                                AND claimed_at <= now() - make_interval(secs => :lease)
                            )
                          )
                        ORDER BY created_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT :limit
                    )
                    UPDATE outbox_events AS event
                    SET status = 'processing',
                        attempts = event.attempts + 1,
                        claimed_at = now(),
                        last_error_code = NULL
                    FROM claimable
                    WHERE event.id = claimable.id
                    RETURNING event.id, event.event_type, event.resource_type,
                              event.resource_id, event.resource_version,
                              event.attempts
                    """
                ),
                {"lease": self.lease_seconds, "limit": limit},
            ).all()
        return [OutboxEvent(*row) for row in rows]

    def complete(self, tenant_id: UUID, event_id: UUID) -> bool:
        with self.database.transaction(tenant_id) as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE outbox_events
                    SET status = 'completed', completed_at = now()
                    WHERE id = :id AND status = 'processing'
                    """
                ),
                {"id": event_id},
            )
            return result.rowcount == 1

    def retry(
        self,
        tenant_id: UUID,
        event_id: UUID,
        error_code: str,
        delay_seconds: int = 0,
    ) -> bool:
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        with self.database.transaction(tenant_id) as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE outbox_events
                    SET status = 'pending',
                        available_at = now() + make_interval(secs => :delay),
                        claimed_at = NULL,
                        last_error_code = :error_code
                    WHERE id = :id AND status = 'processing'
                    """
                ),
                {"id": event_id, "delay": delay_seconds, "error_code": error_code},
            )
            return result.rowcount == 1

    def status(self, tenant_id: UUID, event_id: UUID) -> OutboxStatus | None:
        with self.database.transaction(tenant_id) as connection:
            row = connection.execute(
                text(
                    """
                    SELECT status, attempts, last_error_code
                    FROM outbox_events
                    WHERE id = :id
                    """
                ),
                {"id": event_id},
            ).one_or_none()
        return OutboxStatus(*row) if row else None

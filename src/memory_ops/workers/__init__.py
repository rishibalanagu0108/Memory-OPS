"""Transactional outbox claiming and status operations."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from memory_ops.persistence import TenantDatabase
from memory_ops.lifecycle import PurgeService, PurgeStatus
from memory_ops.retrieval.embeddings import (
    EmbeddingModel,
    EmbeddingPolicy,
    EmbeddingProvider,
    vector_literal,
)


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


class PurgeWorker:
    def __init__(self, database: TenantDatabase) -> None:
        self.outbox = OutboxWorker(database)
        self.purge = PurgeService(database)

    def process(self, tenant_id: UUID, event: OutboxEvent) -> PurgeStatus:
        if (
            event.event_type != "user_memory.purge.requested"
            or event.resource_type != "user_memory"
            or event.resource_version is None
        ):
            raise ValueError("unsupported purge event")
        status = self.purge.purge_all(tenant_id, event.resource_id)
        self.outbox.complete(tenant_id, event.id)
        return status


@dataclass(frozen=True)
class EmbeddingArtifact:
    memory_id: UUID
    canonical_version_id: UUID
    index_generation: str
    model: EmbeddingModel


class EmbeddingWorker:
    def __init__(
        self,
        database: TenantDatabase,
        provider: EmbeddingProvider,
        policy: EmbeddingPolicy,
        index_generation: str,
    ) -> None:
        if not index_generation.strip() or len(index_generation) > 255:
            raise ValueError("index generation must contain 1 to 255 characters")
        self.database = database
        self.outbox = OutboxWorker(database)
        self.provider = provider
        self.policy = policy
        self.index_generation = index_generation

    def process(self, tenant_id: UUID, event: OutboxEvent) -> EmbeddingArtifact | None:
        if (
            event.event_type not in {
                "user_memory.version.created",
                "user_memory.version.corrected",
            }
            or event.resource_type != "user_memory"
            or event.resource_version is None
        ):
            raise ValueError("unsupported embedding event")

        with self.database.transaction(tenant_id) as connection:
            source = connection.execute(
                text(
                    """
                    SELECT v.original_statement, v.sensitivity
                    FROM user_memories m
                    JOIN user_memory_versions v ON v.id = m.current_version_id
                    WHERE m.id = :memory_id
                      AND v.id = :version_id
                      AND m.lifecycle = 'active'
                      AND v.valid_from <= now()
                      AND (v.valid_to IS NULL OR v.valid_to > now())
                      AND (v.retention_until IS NULL OR v.retention_until > now())
                    """
                ),
                {"memory_id": event.resource_id, "version_id": event.resource_version},
            ).one_or_none()

        if source is None or not self.policy.allows(self.provider.metadata, source.sensitivity):
            self.outbox.complete(tenant_id, event.id)
            return None

        embedding = vector_literal(
            self.provider.embed(source.original_statement),
            self.provider.metadata.dimensions,
        )
        model = self.provider.metadata
        with self.database.transaction(tenant_id) as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO user_memory_embeddings (
                        tenant_id, memory_id, canonical_version_id,
                        index_generation, provider, model_name, model_version,
                        embedding
                    )
                    SELECT :tenant_id, m.id, v.id, :index_generation,
                           :provider, :model_name, :model_version,
                           CAST(:embedding AS vector)
                    FROM user_memories m
                    JOIN user_memory_versions v ON v.id = m.current_version_id
                    WHERE m.id = :memory_id
                      AND v.id = :version_id
                      AND m.lifecycle = 'active'
                      AND v.valid_from <= now()
                      AND (v.valid_to IS NULL OR v.valid_to > now())
                      AND (v.retention_until IS NULL OR v.retention_until > now())
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "memory_id": event.resource_id,
                    "version_id": event.resource_version,
                    "index_generation": self.index_generation,
                    "provider": model.provider,
                    "model_name": model.name,
                    "model_version": model.version,
                    "embedding": embedding,
                },
            )
            exists = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM user_memory_embeddings e
                        JOIN user_memories m ON m.id = e.memory_id
                        WHERE e.canonical_version_id = :version_id
                          AND e.index_generation = :index_generation
                          AND e.provider = :provider
                          AND e.model_name = :model_name
                          AND e.model_version = :model_version
                          AND m.current_version_id = e.canonical_version_id
                          AND m.lifecycle = 'active'
                    )
                    """
                ),
                {
                    "version_id": event.resource_version,
                    "index_generation": self.index_generation,
                    "provider": model.provider,
                    "model_name": model.name,
                    "model_version": model.version,
                },
            ).scalar_one()
        self.outbox.complete(tenant_id, event.id)
        if not exists:
            return None
        return EmbeddingArtifact(
            event.resource_id,
            event.resource_version,
            self.index_generation,
            model,
        )

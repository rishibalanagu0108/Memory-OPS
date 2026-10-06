"""Background worker process for durable outbox events."""

import logging
import signal
from threading import Event
from uuid import UUID

from memory_ops.config import get_settings
from memory_ops.persistence import TenantDatabase, create_database_engine
from memory_ops.retrieval import EmbeddingPolicy, HashEmbeddingProvider
from memory_ops.workers import EmbeddingWorker, OutboxEvent, OutboxWorker, PurgeWorker


LOGGER = logging.getLogger(__name__)
VERSION_EVENTS = {
    "user_memory.version.created",
    "user_memory.version.corrected",
}


def process_once(
    tenant_id: UUID,
    outbox: OutboxWorker,
    embeddings: EmbeddingWorker,
    purges: PurgeWorker,
    *,
    limit: int = 100,
    retry_delay_seconds: int = 5,
) -> int:
    """Claim and dispatch one tenant-scoped batch."""

    events = outbox.claim(tenant_id, limit)
    for event in events:
        try:
            _dispatch(tenant_id, event, embeddings, purges)
        except Exception as error:
            error_code = type(error).__name__[:100]
            outbox.retry(
                tenant_id,
                event.id,
                error_code,
                delay_seconds=retry_delay_seconds,
            )
            LOGGER.warning(
                "worker event scheduled for retry",
                extra={
                    "tenant_id": str(tenant_id),
                    "event_id": str(event.id),
                    "event_type": event.event_type,
                    "error_code": error_code,
                },
            )
    return len(events)


def _dispatch(
    tenant_id: UUID,
    event: OutboxEvent,
    embeddings: EmbeddingWorker,
    purges: PurgeWorker,
) -> None:
    if event.event_type in VERSION_EVENTS:
        embeddings.process(tenant_id, event)
        return
    if event.event_type == "user_memory.purge.requested":
        purges.process(tenant_id, event)
        return
    raise ValueError("unsupported outbox event type")


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    if settings.api_tenant_id is None:
        raise RuntimeError("worker requires MEMORY_OPS_API_TENANT_ID")

    engine = create_database_engine(settings.database_url)
    database = TenantDatabase(engine)
    outbox = OutboxWorker(database)
    embeddings = EmbeddingWorker(
        database,
        HashEmbeddingProvider(),
        EmbeddingPolicy({"local": frozenset({"normal", "sensitive"})}),
        "generation-1",
    )
    purges = PurgeWorker(database)
    stopped = Event()

    def stop(*_: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOGGER.info(
        "worker ready",
        extra={"service": settings.service_name, "environment": settings.environment},
    )
    try:
        while not stopped.is_set():
            processed = process_once(
                settings.api_tenant_id,
                outbox,
                embeddings,
                purges,
            )
            if not processed:
                stopped.wait(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

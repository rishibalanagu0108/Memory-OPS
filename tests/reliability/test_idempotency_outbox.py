from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text

from memory_ops.config import Settings
from memory_ops.persistence import (
    TenantDatabase,
    create_database_engine,
    upgrade_database,
)
from memory_ops.persistence.writes import (
    IdempotencyConflict,
    IdempotentWriter,
    OutboxMessage,
    WriteReceipt,
)
from memory_ops.workers import OutboxWorker


TENANT_A = UUID("10000000-0000-0000-0000-000000000001")
TENANT_B = UUID("10000000-0000-0000-0000-000000000002")
WORKSPACE = UUID("10000000-0000-0000-0000-000000000010")
CONCURRENT_WORKSPACE = UUID("10000000-0000-0000-0000-000000000011")
WORKSPACE_B = UUID("10000000-0000-0000-0000-000000000020")


@pytest.fixture(scope="module")
def engine() -> Engine:
    database = create_database_engine(Settings().database_url)
    upgrade_database(database)
    with database.begin() as connection:
        connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
        connection.execute(
            text("DELETE FROM idempotency_records WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
        connection.execute(
            text("DELETE FROM workspaces WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
        connection.execute(
            text("DELETE FROM tenants WHERE id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:a), (:b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
    yield database
    database.dispose()


def create_workspace(
    calls: list[UUID],
) -> Callable[[Connection], tuple[WriteReceipt, list[OutboxMessage]]]:
    def mutation(connection: Connection) -> tuple[WriteReceipt, list[OutboxMessage]]:
        calls.append(WORKSPACE)
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant)"),
            {"id": WORKSPACE, "tenant": TENANT_A},
        )
        receipt = WriteReceipt("workspace", WORKSPACE)
        return receipt, [OutboxMessage("workspace.created", "workspace", WORKSPACE)]

    return mutation


def test_retry_returns_original_receipt_without_duplicate_mutation(
    engine: Engine,
) -> None:
    writer = IdempotentWriter(TenantDatabase(engine))
    calls: list[UUID] = []

    first = writer.execute(
        TENANT_A, "create-workspace", "workspace.create", b'{"name":"A"}', create_workspace(calls)
    )
    replay = writer.execute(
        TENANT_A,
        "create-workspace",
        "workspace.create",
        b'{"name":"A"}',
        lambda _: pytest.fail("a replay must not execute the mutation"),
    )

    with engine.begin() as connection:
        outbox_count = connection.execute(
            text("SELECT count(*) FROM outbox_events WHERE tenant_id = :tenant"),
            {"tenant": TENANT_A},
        ).scalar_one()
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.receipt == first.receipt
    assert calls == [WORKSPACE]
    assert outbox_count == 1


def test_reused_key_with_different_request_is_rejected(engine: Engine) -> None:
    writer = IdempotentWriter(TenantDatabase(engine))

    with pytest.raises(IdempotencyConflict):
        writer.execute(
            TENANT_A,
            "create-workspace",
            "workspace.create",
            b'{"name":"different"}',
            lambda _: pytest.fail("a conflicting retry must not execute the mutation"),
        )


def test_concurrent_retries_execute_the_mutation_once(engine: Engine) -> None:
    writer = IdempotentWriter(TenantDatabase(engine))
    entered = Event()
    second_started = Event()
    release = Event()
    calls: list[UUID] = []

    def mutation(connection: Connection) -> tuple[WriteReceipt, list[OutboxMessage]]:
        calls.append(CONCURRENT_WORKSPACE)
        entered.set()
        assert release.wait(timeout=2)
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant)"),
            {"id": CONCURRENT_WORKSPACE, "tenant": TENANT_A},
        )
        receipt = WriteReceipt("workspace", CONCURRENT_WORKSPACE)
        return receipt, [
            OutboxMessage("workspace.created", "workspace", CONCURRENT_WORKSPACE)
        ]

    def retry():
        second_started.set()
        return writer.execute(
            TENANT_A, "concurrent-write", "workspace.create", b"{}", mutation
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            writer.execute,
            TENANT_A,
            "concurrent-write",
            "workspace.create",
            b"{}",
            mutation,
        )
        assert entered.wait(timeout=2)
        second = pool.submit(retry)
        assert second_started.wait(timeout=2)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert calls == [CONCURRENT_WORKSPACE]
    assert sorted(result.replayed for result in results) == [False, True]


def test_failed_mutation_rolls_back_idempotency_and_outbox(engine: Engine) -> None:
    writer = IdempotentWriter(TenantDatabase(engine))

    def fail(_: Connection) -> tuple[WriteReceipt, list[OutboxMessage]]:
        raise RuntimeError("simulated canonical write failure")

    with pytest.raises(RuntimeError, match="simulated canonical write failure"):
        writer.execute(TENANT_B, "failed-write", "workspace.create", b"{}", fail)

    with engine.begin() as connection:
        record_count = connection.execute(
            text(
                "SELECT count(*) FROM idempotency_records "
                "WHERE tenant_id = :tenant AND idempotency_key = 'failed-write'"
            ),
            {"tenant": TENANT_B},
        ).scalar_one()
        outbox_count = connection.execute(
            text("SELECT count(*) FROM outbox_events WHERE tenant_id = :tenant"),
            {"tenant": TENANT_B},
        ).scalar_one()
    assert record_count == outbox_count == 0


def test_worker_failure_retries_durable_event_and_reports_status(engine: Engine) -> None:
    writer = IdempotentWriter(TenantDatabase(engine))
    worker = OutboxWorker(TenantDatabase(engine))

    def mutation(connection: Connection) -> tuple[WriteReceipt, list[OutboxMessage]]:
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant)"),
            {"id": WORKSPACE_B, "tenant": TENANT_B},
        )
        receipt = WriteReceipt("workspace", WORKSPACE_B)
        return receipt, [OutboxMessage("workspace.created", "workspace", WORKSPACE_B)]

    writer.execute(TENANT_B, "worker-retry", "workspace.create", b"{}", mutation)
    claimed = worker.claim(TENANT_B)
    assert len(claimed) == 1
    event = claimed[0]
    assert event.attempts == 1
    assert worker.status(TENANT_A, event.id) is None

    assert worker.retry(TENANT_B, event.id, "index_unavailable") is True
    retry_status = worker.status(TENANT_B, event.id)
    assert retry_status is not None
    assert retry_status.status == "pending"
    assert retry_status.last_error_code == "index_unavailable"

    reclaimed = worker.claim(TENANT_B)
    assert [item.id for item in reclaimed] == [event.id]
    assert reclaimed[0].attempts == 2
    assert worker.complete(TENANT_B, event.id) is True
    assert worker.claim(TENANT_B) == []
    assert worker.status(TENANT_B, event.id).status == "completed"

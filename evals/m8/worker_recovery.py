"""Measure bounded worker recovery against the isolated M8 backlog."""

from __future__ import annotations

from hashlib import md5, sha256
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import UUID

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evals/m8/worker-recovery-profile.json"
SOURCE_PATHS = (
    "src/memory_ops/worker.py",
    "src/memory_ops/config.py",
    "evals/m8/worker_recovery.py",
)


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def deterministic_uuid(label: str) -> UUID:
    return UUID(md5(label.encode(), usedforsecurity=False).hexdigest())


def state(connection, tenant_id) -> dict:
    return dict(
        connection.execute(
            text(
                """
                SELECT count(*) FILTER (WHERE status != 'completed') AS remaining,
                       count(*) FILTER (WHERE status = 'pending') AS pending,
                       count(*) FILTER (WHERE status = 'processing') AS processing,
                       count(*) FILTER (WHERE last_error_code IS NOT NULL) AS retries
                FROM outbox_events
                WHERE tenant_id = :tenant_id
                """
            ),
            {"tenant_id": tenant_id},
        ).mappings().one()
    )


def acknowledged_write_loss(connection, tenant_id) -> int:
    return connection.execute(
        text(
            """
            SELECT count(*)
            FROM idempotency_records i
            LEFT JOIN outbox_events o
              ON o.tenant_id = i.tenant_id
             AND o.resource_id = i.resource_id
             AND o.resource_version = i.resource_version
             AND o.event_type = 'user_memory.version.created'
            WHERE i.tenant_id = :tenant_id
              AND i.idempotency_key LIKE 'm8-load-%'
              AND i.operation = 'user_memory.remember'
              AND i.completed_at IS NOT NULL
              AND o.id IS NULL
            """
        ),
        {"tenant_id": tenant_id},
    ).scalar_one()


def stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=180)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def run(duration_seconds: int = 300, concurrency: int = 8) -> dict:
    direct_url = os.environ["M8_DATABASE_URL_UNPOOLED"]
    pooled_url = os.environ["M8_DATABASE_URL_POOLED"]
    engine = create_engine(
        direct_url.replace("postgresql://", "postgresql+psycopg://", 1),
        pool_pre_ping=True,
    )
    tenant_id = deterministic_uuid("m8-tenant-1")
    environment = os.environ | {
        "MEMORY_OPS_DATABASE_URL": pooled_url,
        "MEMORY_OPS_MIGRATION_DATABASE_URL": direct_url,
        "MEMORY_OPS_ENVIRONMENT": "production",
        "MEMORY_OPS_API_TOKEN": "m8-worker-recovery",
        "MEMORY_OPS_API_TENANT_ID": str(tenant_id),
        "MEMORY_OPS_API_WORKSPACE_ID": str(deterministic_uuid("m8-workspace-1")),
        "MEMORY_OPS_API_PRINCIPAL_ID": str(deterministic_uuid("m8-principal-1")),
        "MEMORY_OPS_WORKER_BATCH_SIZE": "100",
        "MEMORY_OPS_WORKER_CONCURRENCY": str(concurrency),
        "MEMORY_OPS_WORKER_IDLE_SECONDS": "0.05",
        "MEMORY_OPS_WORKER_RETRY_DELAY_SECONDS": "0",
    }
    worker: subprocess.Popen | None = None
    started = time.monotonic()
    try:
        with engine.connect() as connection:
            before = state(connection, tenant_id)
        if before["remaining"] < 257:
            raise RuntimeError("worker recovery profile requires at least 257 events")
        worker = subprocess.Popen(
            [sys.executable, "-m", "memory_ops.worker"],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = started + duration_seconds
        current = before
        while time.monotonic() < deadline and current["remaining"]:
            time.sleep(min(5, max(0, deadline - time.monotonic())))
            if worker.poll() is not None:
                raise RuntimeError(f"recovery worker exited with {worker.returncode}")
            with engine.connect() as connection:
                current = state(connection, tenant_id)
            print(
                json.dumps(
                    {
                        "recovery_elapsed_seconds": round(time.monotonic() - started),
                        "remaining": current["remaining"],
                    }
                ),
                flush=True,
            )
        stop(worker)
        worker = None
        elapsed = time.monotonic() - started
        with engine.connect() as connection:
            after = state(connection, tenant_id)
            write_loss = acknowledged_write_loss(connection, tenant_id)
    finally:
        stop(worker)
        engine.dispose()

    drained = before["remaining"] - after["remaining"]
    observed_rate = drained / elapsed
    required_rate = 257 / 300
    result = {
        "milestone": "m8-13",
        "status": "measured_candidate",
        "inputs": {path: digest(ROOT / path) for path in SOURCE_PATHS},
        "profile": {
            "duration_seconds": elapsed,
            "worker_processes": 1,
            "worker_concurrency": concurrency,
            "batch_size": 100,
        },
        "before": before,
        "after": after,
        "recovery": {
            "drained": drained,
            "observed_events_per_second": observed_rate,
            "required_events_per_second": required_rate,
            "published_257_event_backlog_within_300_seconds": observed_rate >= required_rate,
        },
        "safety": {"acknowledged_write_loss_count": write_loss},
        "limits": [
            "This isolated five-minute drain measures sustained recovery rate, not the protected hour-long M8 release profile.",
            "The run uses the accumulated representative backlog rather than replaying a worker outage during API load.",
            "This evidence does not claim production readiness or change production behavior.",
        ],
    }
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    run()

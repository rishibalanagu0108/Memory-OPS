"""Provision and exercise the declared M8 workload on an ephemeral database."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from hashlib import md5
import json
import os
import signal
import subprocess
import sys
from threading import Lock
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[2]
PROGRESS = ROOT / "evals/m8/fullscale-progress.json"
FULLSCALE_RESULT = ROOT / "evals/m8/fullscale-result.json"
MIB = 1024 * 1024


def _engine():
    url = os.environ["M8_DATABASE_URL_UNPOOLED"].replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    return create_engine(url, pool_pre_ping=True)


def database_bytes(connection) -> int:
    return connection.execute(
        text("SELECT pg_database_size(current_database())")
    ).scalar_one()


def seed(memory_count: int, batch_size: int, stop_at_mib: int) -> dict:
    engine = _engine()
    started = time.time()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO tenants (id)
                    SELECT md5('m8-tenant-' || n)::uuid
                    FROM generate_series(1, 100) AS n
                    ON CONFLICT DO NOTHING
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO workspaces (id, tenant_id)
                    SELECT md5('m8-workspace-' || n)::uuid,
                           md5('m8-tenant-' || n)::uuid
                    FROM generate_series(1, 100) AS n
                    ON CONFLICT DO NOTHING
                    """
                )
            )

        # The synthetic IDs are deterministic; count by the dedicated policy marker.
        with engine.connect() as connection:
            seeded = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM user_memory_versions
                    WHERE policy_version = 'm8-fullscale-v1'
                    """
                )
            ).scalar_one()

        while seeded < memory_count:
            end = min(seeded + batch_size, memory_count)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO user_memories (
                            id, tenant_id, workspace_id, subject_id, semantic_type,
                            lifecycle, current_version_id
                        )
                        SELECT md5('m8-memory-' || n)::uuid,
                               md5('m8-tenant-' || (((n - 1) % 100) + 1))::uuid,
                               md5('m8-workspace-' || (((n - 1) % 100) + 1))::uuid,
                               md5('m8-subject-' || (((n - 1) % 1000) + 1))::uuid,
                               'fact', 'active', md5('m8-version-' || n)::uuid
                        FROM generate_series(
                            CAST(:start AS bigint), CAST(:end AS bigint)
                        ) AS n
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {"start": seeded + 1, "end": end},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO user_memory_versions (
                            id, tenant_id, memory_id, version_number,
                            original_statement, normalized_subject,
                            normalized_predicate, normalized_value, qualifiers,
                            valid_from, sensitivity, lifetime, origin, purpose,
                            policy_version, access_scope
                        )
                        SELECT md5('m8-version-' || n)::uuid,
                               md5('m8-tenant-' || (((n - 1) % 100) + 1))::uuid,
                               md5('m8-memory-' || n)::uuid, 1,
                               'memory profile item ' || n,
                               md5('m8-subject-' || (((n - 1) % 1000) + 1)),
                               'fact', to_jsonb(n), '{}'::jsonb, now(),
                               'normal', 'durable', 'explicit',
                               'assistant_context', 'm8-fullscale-v1', '[]'::jsonb
                        FROM generate_series(
                            CAST(:start AS bigint), CAST(:end AS bigint)
                        ) AS n
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {"start": seeded + 1, "end": end},
                )
            seeded = end
            with engine.connect() as connection:
                size = database_bytes(connection)
            progress = {
                "target_memories": memory_count,
                "seeded_memories": seeded,
                "database_bytes": size,
                "elapsed_seconds": round(time.time() - started, 3),
                "capacity_stop": size >= stop_at_mib * MIB and seeded < memory_count,
            }
            PROGRESS.write_text(json.dumps(progress, indent=2) + "\n")
            print(json.dumps(progress), flush=True)
            if progress["capacity_stop"]:
                break
        return progress
    finally:
        engine.dispose()


def seed_documents(document_count: int, batch_size: int) -> dict:
    engine = _engine()
    started = time.time()
    try:
        with engine.connect() as connection:
            seeded = connection.execute(
                text(
                    """
                    SELECT count(*) FROM knowledge_documents
                    WHERE source_id LIKE 'm8-fullscale-%'
                    """
                )
            ).scalar_one()
        while seeded < document_count:
            end = min(seeded + batch_size, document_count)
            parameters = {"start": seeded + 1, "end": end}
            series = (
                "generate_series(CAST(:start AS bigint), "
                "CAST(:end AS bigint)) AS n"
            )
            with engine.begin() as connection:
                connection.execute(
                    text(
                        f"""
                        INSERT INTO knowledge_documents (
                            id, tenant_id, workspace_id, source_id, title,
                            owner_principal_id, lifecycle, ingestion_status,
                            current_version_id, current_acl_revision_id
                        )
                        SELECT md5('m8-document-' || n)::uuid,
                               md5('m8-tenant-' || (((n - 1) % 100) + 1))::uuid,
                               md5('m8-workspace-' || (((n - 1) % 100) + 1))::uuid,
                               'm8-fullscale-' || n, 'Policy ' || n,
                               md5('m8-principal-' || (((n - 1) % 1000) + 1))::uuid,
                               'active', 'ready',
                               md5('m8-document-version-' || n)::uuid,
                               md5('m8-document-acl-' || n)::uuid
                        FROM {series}
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    parameters,
                )
                connection.execute(
                    text(
                        f"""
                        INSERT INTO knowledge_document_versions (
                            id, tenant_id, document_id, version_number,
                            storage_bucket, object_key, storage_encryption,
                            media_type, byte_size, content_hash,
                            publication_status, effective_from,
                            access_policy_version
                        )
                        SELECT md5('m8-document-version-' || n)::uuid,
                               md5('m8-tenant-' || (((n - 1) % 100) + 1))::uuid,
                               md5('m8-document-' || n)::uuid, 1,
                               'm8-fullscale', 'documents/' || n, 'AES256',
                               'text/plain', 10737419,
                               md5('m8-document-content-' || n) ||
                                   md5('m8-document-content-2-' || n),
                               'published', now(), 'm8-policy-v1'
                        FROM {series}
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    parameters,
                )
                connection.execute(
                    text(
                        f"""
                        INSERT INTO knowledge_document_acl_revisions (
                            id, tenant_id, document_id, revision_number,
                            policy_version
                        )
                        SELECT md5('m8-document-acl-' || n)::uuid,
                               md5('m8-tenant-' || (((n - 1) % 100) + 1))::uuid,
                               md5('m8-document-' || n)::uuid, 1, 'm8-policy-v1'
                        FROM {series}
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    parameters,
                )
                connection.execute(
                    text(
                        f"""
                        INSERT INTO knowledge_document_acl_grants (
                            tenant_id, document_id, acl_revision_id, principal_id
                        )
                        SELECT md5('m8-tenant-' || (((n - 1) % 100) + 1))::uuid,
                               md5('m8-document-' || n)::uuid,
                               md5('m8-document-acl-' || n)::uuid,
                               md5('m8-principal-' || (((n - 1) % 1000) + 1))::uuid
                        FROM {series}
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    parameters,
                )
                connection.execute(
                    text(
                        f"""
                        INSERT INTO knowledge_document_chunks (
                            id, tenant_id, document_id, document_version_id,
                            ordinal, content, content_hash, locator_kind,
                            locator_path, start_line, end_line, structure_path,
                            index_generation, projection_model,
                            projection_model_version
                        )
                        SELECT md5('m8-document-chunk-' || n)::uuid,
                               md5('m8-tenant-' || (((n - 1) % 100) + 1))::uuid,
                               md5('m8-document-' || n)::uuid,
                               md5('m8-document-version-' || n)::uuid, 0,
                               'organizational memory policy item ' || n,
                               md5('m8-chunk-content-' || n) ||
                                   md5('m8-chunk-content-2-' || n),
                               'plain_text_lines', 'lines:1-1', 1, 1,
                               '[]'::json, 'knowledge-v1', 'deterministic', '1'
                        FROM {series}
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    parameters,
                )
            seeded = end
            with engine.connect() as connection:
                size = database_bytes(connection)
            progress = {
                "target_documents": document_count,
                "seeded_documents": seeded,
                "logical_source_bytes": seeded * 10_737_419,
                "database_bytes": size,
                "elapsed_seconds": round(time.time() - started, 3),
            }
            PROGRESS.write_text(json.dumps(progress, indent=2) + "\n")
            print(json.dumps(progress), flush=True)
        return progress
    finally:
        engine.dispose()


def deterministic_uuid(label: str) -> UUID:
    return UUID(md5(label.encode(), usedforsecurity=False).hexdigest())


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * quantile))]


def rss_bytes(process: subprocess.Popen) -> int:
    try:
        status = Path(f"/proc/{process.pid}/status").read_text().splitlines()
        return int(next(line for line in status if line.startswith("VmRSS:")).split()[1]) * 1024
    except (FileNotFoundError, StopIteration):
        return 0


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def run_load(duration_seconds: int, base_url: str) -> dict:
    tenant_id = deterministic_uuid("m8-tenant-1")
    workspace_id = deterministic_uuid("m8-workspace-1")
    subject_id = deterministic_uuid("m8-subject-1")
    principal_id = deterministic_uuid("m8-principal-1")
    pooled_url = os.environ["M8_DATABASE_URL_POOLED"]
    direct_url = os.environ["M8_DATABASE_URL_UNPOOLED"]
    token = f"m8-load-{uuid4()}"
    environment = os.environ | {
        "MEMORY_OPS_DATABASE_URL": pooled_url,
        "MEMORY_OPS_MIGRATION_DATABASE_URL": direct_url,
        "MEMORY_OPS_ENVIRONMENT": "production",
        "MEMORY_OPS_API_TOKEN": token,
        "MEMORY_OPS_API_TENANT_ID": str(tenant_id),
        "MEMORY_OPS_API_WORKSPACE_ID": str(workspace_id),
        "MEMORY_OPS_API_PRINCIPAL_ID": str(principal_id),
        "MEMORY_OPS_API_MAX_IN_FLIGHT": "100",
        "MEMORY_OPS_WORKER_BATCH_SIZE": "100",
        "MEMORY_OPS_WORKER_IDLE_SECONDS": "0.05",
    }
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "memory_ops.api:app", "--host", "127.0.0.1", "--port", base_url.rsplit(":", 1)[1], "--workers", "4", "--log-level", "warning"],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    worker: subprocess.Popen | None = subprocess.Popen(
        [sys.executable, "-m", "memory_ops.worker"], cwd=ROOT, env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    headers = {"Authorization": f"Bearer {token}"}
    latencies: dict[str, list[float]] = {
        name: []
        for name in (
            "canonical_write", "memory_search", "memory_inspect",
            "context_build", "knowledge_search", "operation_status",
        )
    }
    statuses: dict[int, int] = {}
    errors: list[dict] = []
    operations: dict[str, float] = {}
    operation_ids: list[str] = []
    index_lags: list[float] = []
    state_lock = Lock()
    revoked_disclosures = 0
    isolation_disclosures = 0
    fault_started = fault_ended = None
    start_rss = {"api": 0, "worker": 0}
    started = time.monotonic()

    def request(client: httpx.Client, sequence: int, measured: bool) -> None:
        nonlocal revoked_disclosures, isolation_disclosures
        slot = sequence % 100
        operation = (
            "canonical_write" if slot < 15 else
            "memory_search" if slot < 45 else
            "memory_inspect" if slot < 55 else
            "context_build" if slot < 80 else
            "knowledge_search" if slot < 90 else
            "operation_status"
        )
        path = f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}"
        method = "GET"
        kwargs: dict = {"headers": headers}
        if operation == "canonical_write":
            method = "POST"
            path += "/memories"
            kwargs["headers"] = headers | {"Idempotency-Key": f"{token}-{sequence}"}
            kwargs["json"] = {
                "subject_id": str(subject_id), "semantic_type": "fact",
                "statement": f"load generated memory {sequence}",
                "purpose": "assistant_context",
            }
        elif operation in {"memory_search", "context_build"}:
            method = "POST"
            path += "/context"
            kwargs["json"] = {
                "subject_id": str(subject_id), "query": "memory profile",
                "purpose": "assistant_context", "token_budget": 256,
            }
        elif operation == "memory_inspect":
            path += f"/memories/{deterministic_uuid('m8-memory-1')}"
        elif operation == "knowledge_search":
            method = "POST"
            path += "/knowledge/search"
            kwargs["json"] = {"query": "organizational memory policy", "limit": 10}
        else:
            with state_lock:
                operation_id = operation_ids[sequence % len(operation_ids)] if operation_ids else None
            path += f"/operations/{operation_id or deterministic_uuid('missing-operation')}"
        before = time.perf_counter()
        try:
            response = client.request(method, path, **kwargs)
            elapsed_ms = (time.perf_counter() - before) * 1000
            with state_lock:
                if measured:
                    latencies[operation].append(elapsed_ms)
                    statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
                if measured and response.status_code >= 500 and len(errors) < 10:
                    errors.append({"operation": operation, "status": response.status_code})
                if operation == "canonical_write" and response.status_code == 201:
                    payload = response.json()
                    operations[payload["operation_id"]] = time.monotonic()
                    operation_ids.append(payload["operation_id"])
                elif operation == "operation_status" and response.status_code == 200:
                    payload = response.json()
                    created = operations.get(payload["operation_id"])
                    if created is not None and payload["status"] == "completed":
                        index_lags.append(time.monotonic() - created)
                        operations.pop(payload["operation_id"], None)
        except httpx.HTTPError as error:
            if measured:
                with state_lock:
                    statuses[0] = statuses.get(0, 0) + 1
                    if len(errors) < 10:
                        errors.append({"operation": operation, "error": type(error).__name__})

    try:
        with httpx.Client(base_url=base_url, timeout=120, limits=httpx.Limits(max_connections=100, max_keepalive_connections=50)) as client:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("API did not become ready")
            start_rss = {"api": rss_bytes(api), "worker": rss_bytes(worker)}
            total = 0
            pending = set()
            next_request = time.monotonic()
            next_report = started + 60
            with ThreadPoolExecutor(max_workers=50) as executor:
                while time.monotonic() - started < duration_seconds:
                    elapsed = time.monotonic() - started
                    if duration_seconds >= 600 and 1800 <= elapsed < 1860:
                        if worker is not None and worker.poll() is None:
                            fault_started = time.monotonic()
                            stop_process(worker)
                            worker = None
                    elif duration_seconds >= 600 and elapsed >= 1860 and worker is None:
                        worker = subprocess.Popen(
                            [sys.executable, "-m", "memory_ops.worker"], cwd=ROOT, env=environment,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                        fault_ended = time.monotonic()
                    rate = 25
                    if 60 <= elapsed < 120:
                        rate = 50
                    elif 120 <= elapsed < 180:
                        rate = 100
                    now = time.monotonic()
                    if now >= next_request:
                        if len(pending) >= 50:
                            done, pending = wait(pending, return_when=FIRST_COMPLETED)
                            for future in done:
                                future.result()
                        pending.add(executor.submit(request, client, total, elapsed >= 60))
                        total += 1
                        next_request = max(next_request + 1 / rate, now)
                    else:
                        time.sleep(min(next_request - now, 0.01))
                    if now >= next_report:
                        print(json.dumps({"load_elapsed_seconds": round(elapsed), "requests_submitted": total, "in_flight": len(pending)}), flush=True)
                        next_report += 60
                for future in pending:
                    future.result()

            other_tenant = deterministic_uuid("m8-tenant-2")
            other_workspace = deterministic_uuid("m8-workspace-2")
            isolation = client.get(
                f"/v1/tenants/{other_tenant}/workspaces/{other_workspace}/memories",
                headers=headers,
            )
            isolation_disclosures += int(isolation.status_code != 403)
            created = client.post(
                f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}/memories",
                headers=headers | {"Idempotency-Key": f"m8-delete-{uuid4()}"},
                json={"subject_id": str(subject_id), "semantic_type": "fact", "statement": "deletion probe", "purpose": "assistant_context"},
            )
            if created.status_code == 201:
                memory_id = created.json()["memory_id"]
                deleted = client.delete(
                    f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}/memories/{memory_id}",
                    headers=headers | {"Idempotency-Key": f"m8-forget-{uuid4()}"},
                    params={"subject_id": str(subject_id)},
                )
                disclosed = client.get(
                    f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}/memories/{memory_id}",
                    headers=headers,
                )
                revoked_disclosures += int(deleted.status_code != 202 or disclosed.status_code != 404)
        elapsed = time.monotonic() - started
        all_latencies = [value for values in latencies.values() for value in values]
        successes = sum(count for status, count in statuses.items() if 200 <= status < 400)
        requests = sum(statuses.values())
        result = {
            "duration_seconds": round(elapsed, 3),
            "requests": requests,
            "throughput_qps": requests / elapsed,
            "error_rate": (requests - successes) / requests if requests else 1.0,
            "status_counts": {str(key): value for key, value in sorted(statuses.items())},
            "latency_ms": {
                "p50": percentile(all_latencies, 0.50),
                "p95": percentile(all_latencies, 0.95),
                "p99": percentile(all_latencies, 0.99),
                "by_operation_p95": {name: percentile(values, 0.95) for name, values in latencies.items()},
            },
            "index_lag_seconds": {
                "samples": len(index_lags),
                "p95": percentile(index_lags, 0.95),
                "p99": percentile(index_lags, 0.99),
            },
            "fault": {
                "worker_outage_seconds": (fault_ended - fault_started) if fault_started and fault_ended else 0,
                "acknowledged_write_loss_count": 0,
            },
            "cross_tenant_disclosure_count": isolation_disclosures,
            "post_revocation_disclosure_count": revoked_disclosures,
            "resource_growth_bytes": {
                "api_rss": rss_bytes(api) - start_rss["api"],
                "worker_rss": rss_bytes(worker) - start_rss["worker"] if worker else 0,
            },
            "errors": errors,
        }
        FULLSCALE_RESULT.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2), flush=True)
        return result
    finally:
        stop_process(worker)
        stop_process(api)


def finalize(max_recovery_seconds: int) -> dict:
    result = json.loads(FULLSCALE_RESULT.read_text())
    engine = _engine()
    tenant_id = deterministic_uuid("m8-tenant-1")
    workspace_id = deterministic_uuid("m8-workspace-1")
    principal_id = deterministic_uuid("m8-principal-1")
    pooled_url = os.environ["M8_DATABASE_URL_POOLED"]
    direct_url = os.environ["M8_DATABASE_URL_UNPOOLED"]
    environment = os.environ | {
        "MEMORY_OPS_DATABASE_URL": pooled_url,
        "MEMORY_OPS_MIGRATION_DATABASE_URL": direct_url,
        "MEMORY_OPS_ENVIRONMENT": "production",
        "MEMORY_OPS_API_TOKEN": "m8-finalize-worker",
        "MEMORY_OPS_API_TENANT_ID": str(tenant_id),
        "MEMORY_OPS_API_WORKSPACE_ID": str(workspace_id),
        "MEMORY_OPS_API_PRINCIPAL_ID": str(principal_id),
        "MEMORY_OPS_WORKER_BATCH_SIZE": "100",
        "MEMORY_OPS_WORKER_IDLE_SECONDS": "0.05",
    }

    def evidence(connection) -> dict:
        return dict(
            connection.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM user_memory_versions
                       WHERE policy_version = 'm8-fullscale-v1') AS memories,
                      (SELECT count(*) FROM knowledge_documents
                       WHERE source_id LIKE 'm8-fullscale-%') AS documents,
                      (SELECT COALESCE(sum(byte_size), 0)
                       FROM knowledge_document_versions v
                       JOIN knowledge_documents d ON d.id = v.document_id
                       WHERE d.source_id LIKE 'm8-fullscale-%') AS logical_source_bytes,
                      (SELECT count(*) FROM idempotency_records
                       WHERE idempotency_key LIKE 'm8-load-%'
                         AND operation = 'user_memory.remember'
                         AND completed_at IS NOT NULL) AS acknowledged_writes,
                      (SELECT count(*)
                       FROM idempotency_records i
                       LEFT JOIN outbox_events o
                         ON o.tenant_id = i.tenant_id
                        AND o.resource_id = i.resource_id
                        AND o.resource_version = i.resource_version
                        AND o.event_type = 'user_memory.version.created'
                       WHERE i.idempotency_key LIKE 'm8-load-%'
                         AND i.operation = 'user_memory.remember'
                         AND i.completed_at IS NOT NULL
                         AND o.id IS NULL) AS acknowledged_write_loss_count,
                      (SELECT count(*)
                       FROM outbox_events o
                       JOIN idempotency_records i
                         ON i.tenant_id = o.tenant_id
                        AND i.resource_id = o.resource_id
                        AND i.resource_version = o.resource_version
                       WHERE i.idempotency_key LIKE 'm8-load-%'
                         AND o.status != 'completed') AS remaining_work,
                      (SELECT count(*)
                       FROM memory_deletion_tombstones t
                       JOIN idempotency_records i
                         ON i.tenant_id = t.tenant_id
                        AND i.resource_id = t.memory_id
                       WHERE i.idempotency_key LIKE 'm8-forget-%'
                         AND t.completed_at IS NOT NULL) AS completed_deletions,
                      pg_database_size(current_database()) AS database_bytes
                    """
                )
            ).mappings().one()
        )

    worker = None
    recovery_started = time.monotonic()
    try:
        with engine.connect() as connection:
            before = evidence(connection)
        if before["remaining_work"]:
            worker = subprocess.Popen(
                [sys.executable, "-m", "memory_ops.worker"], cwd=ROOT,
                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            while time.monotonic() - recovery_started < max_recovery_seconds:
                time.sleep(5)
                if worker.poll() is not None:
                    raise RuntimeError(f"recovery worker exited with {worker.returncode}")
                with engine.connect() as connection:
                    current = evidence(connection)
                print(json.dumps({"recovery_elapsed_seconds": round(time.monotonic() - recovery_started), "remaining_work": current["remaining_work"]}), flush=True)
                if current["remaining_work"] == 0:
                    break
        recovery_seconds = time.monotonic() - recovery_started
        with engine.connect() as connection:
            after = evidence(connection)
    finally:
        stop_process(worker)
        engine.dispose()

    branch_rows = json.loads(
        subprocess.run(
            ["neon", "branches", "list", "--project-id", "cold-silence-99209400", "--output", "json"],
            cwd=ROOT, text=True, capture_output=True, check=True,
        ).stdout
    )
    branch = next(row for row in branch_rows if row["name"] == "m8-04-fullscale-20261010")
    compute_seconds = branch["compute_time_seconds"]
    storage_gb = branch["logical_size"] / 1_000_000_000
    launch_equivalent = compute_seconds / 3600 * 0.106
    conservative_run_cost = 2 * result["duration_seconds"] / 3600 * 0.106
    result.update(
        {
            "dataset": {
                "tenant_count": 100,
                "active_principal_count": 1000,
                "canonical_memory_count": int(after["memories"]),
                "organizational_document_count": int(after["documents"]),
                "logical_source_bytes": int(after["logical_source_bytes"]),
                "object_bytes_materialized": False,
                "database_bytes": int(after["database_bytes"]),
            },
            "recovery": {
                "remaining_before_drain": int(before["remaining_work"]),
                "remaining_after_drain": int(after["remaining_work"]),
                "drain_seconds": round(recovery_seconds, 3),
                "completed_deletions": int(after["completed_deletions"]),
            },
            "cost": {
                "plan": "free",
                "billed_usd": 0.0,
                "observed_compute_unit_seconds": compute_seconds,
                "observed_data_transfer_bytes": branch["data_transfer_bytes"],
                "observed_logical_size_bytes": branch["logical_size"],
                "launch_equivalent_run_compute_usd": launch_equivalent,
                "launch_equivalent_variable_usd_per_1000_operations": launch_equivalent / max(result["requests"], 1) * 1000,
                "launch_conservative_run_compute_usd_at_2_cu": conservative_run_cost,
                "launch_conservative_variable_usd_per_1000_operations": conservative_run_cost / max(result["requests"], 1) * 1000,
                "launch_conservative_monthly_usd_at_2_cu": 2 * 744 * 0.106 + storage_gb * 0.35,
                "pricing_source": "https://neon.com/docs/introduction/usage-calculations",
            },
        }
    )
    result["fault"]["acknowledged_write_loss_count"] = int(
        after["acknowledged_write_loss_count"]
    )
    FULLSCALE_RESULT.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("seed", "seed-documents", "run-load", "finalize"))
    parser.add_argument("--memory-count", type=int, default=1_000_000)
    parser.add_argument("--batch-size", type=int, default=25_000)
    parser.add_argument("--stop-at-mib", type=int, default=950)
    parser.add_argument("--document-count", type=int, default=10_000)
    parser.add_argument("--duration-seconds", type=int, default=3_660)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--max-recovery-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.command == "seed":
        seed(args.memory_count, args.batch_size, args.stop_at_mib)
    elif args.command == "seed-documents":
        seed_documents(args.document_count, min(args.batch_size, 1_000))
    elif args.command == "run-load":
        run_load(args.duration_seconds, args.base_url)
    else:
        finalize(args.max_recovery_seconds)


if __name__ == "__main__":
    main()

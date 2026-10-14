"""Run the unchanged M8 workload with host, process, pool, and database signals."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from itertools import count
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Lock, Thread
import time
from uuid import uuid4

import httpx
from sqlalchemy import text


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.m8.fullscale import _engine, deterministic_uuid, percentile, stop_process
from evals.m8.latency_attribution import (
    LEVELS,
    core_flow,
    measured_request,
    request_spec,
    summarize,
)


OUTPUT = ROOT / "evals/m8/capacity-load-result.json"
SOURCE_PATHS = (
    "evals/m8/capacity_instrumented_api.py",
    "evals/m8/capacity_probe.py",
    "evals/m8/instrumented_api.py",
    "evals/m8/latency_attribution.py",
)


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _process_rows() -> dict[int, dict[str, int]]:
    rows: dict[int, dict[str, int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            statm = (entry / "statm").read_text().split()
            rows[int(entry.name)] = {
                "ppid": int(fields[1]),
                "cpu_ticks": int(fields[11]) + int(fields[12]),
                "rss_bytes": int(statm[1]) * os.sysconf("SC_PAGE_SIZE"),
            }
        except (FileNotFoundError, IndexError, PermissionError, ProcessLookupError):
            continue
    return rows


def _tree(rows: dict[int, dict[str, int]], root: int) -> set[int]:
    selected = {root}
    changed = True
    while changed:
        changed = False
        for pid, row in rows.items():
            if row["ppid"] in selected and pid not in selected:
                selected.add(pid)
                changed = True
    return selected


def _group(rows: dict[int, dict[str, int]], root: int) -> dict[str, int]:
    selected = _tree(rows, root)
    return {
        "processes": len(selected & rows.keys()),
        "cpu_ticks": sum(rows[pid]["cpu_ticks"] for pid in selected if pid in rows),
        "rss_bytes": sum(rows[pid]["rss_bytes"] for pid in selected if pid in rows),
    }


def _host() -> dict[str, float]:
    cpu = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    ticks = [int(value) for value in cpu]
    memory = {
        row.split(":", 1)[0]: int(row.split()[1]) * 1024
        for row in Path("/proc/meminfo").read_text().splitlines()
    }
    load = [float(value) for value in Path("/proc/loadavg").read_text().split()[:3]]
    return {
        "cpu_total_ticks": sum(ticks),
        "cpu_idle_ticks": ticks[3] + ticks[4],
        "memory_total_bytes": memory["MemTotal"],
        "memory_available_bytes": memory["MemAvailable"],
        "load_1": load[0],
        "load_5": load[1],
        "load_15": load[2],
    }


def _database_sample(engine) -> dict:
    with engine.connect() as connection:
        activity = dict(
            connection.execute(
                text(
                    """
                    SELECT count(*) AS connections,
                           count(*) FILTER (WHERE state = 'active') AS active,
                           count(*) FILTER (WHERE wait_event IS NOT NULL) AS waiting
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                    """
                )
            ).mappings().one()
        )
        database = dict(
            connection.execute(
                text(
                    """
                    SELECT xact_commit, xact_rollback, blks_read, blks_hit,
                           temp_files, temp_bytes, deadlocks
                    FROM pg_stat_database
                    WHERE datname = current_database()
                    """
                )
            ).mappings().one()
        )
        outbox = dict(
            connection.execute(
                text("SELECT status, count(*) FROM outbox_events GROUP BY status")
            ).all()
        )
        max_connections = int(
            connection.execute(text("SHOW max_connections")).scalar_one()
        )
    return {
        **{key: int(value) for key, value in activity.items()},
        **{key: int(value) for key, value in database.items()},
        "max_connections": max_connections,
        "outbox": {str(key): int(value) for key, value in outbox.items()},
    }


class Sampler:
    def __init__(self, api_pid: int, worker_pid: int, concurrency: list[int]) -> None:
        self.api_pid = api_pid
        self.worker_pid = worker_pid
        self.concurrency = concurrency
        self.samples: list[dict] = []
        self._stop = Event()
        self._thread = Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        engine = _engine()
        previous: dict | None = None
        clock_ticks = os.sysconf("SC_CLK_TCK")
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                rows = _process_rows()
                host = _host()
                api = _group(rows, self.api_pid)
                worker = _group(rows, self.worker_pid)
                sample = {
                    "at_epoch_seconds": time.time(),
                    "concurrency": self.concurrency[0],
                    "host": host,
                    "api": api,
                    "worker": worker,
                }
                try:
                    sample["database"] = _database_sample(engine)
                except Exception as error:
                    sample["database_error"] = type(error).__name__
                if previous is not None:
                    elapsed = max(sample["at_epoch_seconds"] - previous["at_epoch_seconds"], 0.001)
                    total_delta = host["cpu_total_ticks"] - previous["host"]["cpu_total_ticks"]
                    idle_delta = host["cpu_idle_ticks"] - previous["host"]["cpu_idle_ticks"]
                    sample["host"]["cpu_percent"] = (
                        100.0 * (total_delta - idle_delta) / total_delta
                        if total_delta > 0
                        else 0.0
                    )
                    for name in ("api", "worker"):
                        tick_delta = sample[name]["cpu_ticks"] - previous[name]["cpu_ticks"]
                        sample[name]["cpu_percent"] = max(
                            100.0 * tick_delta / clock_ticks / elapsed, 0.0
                        )
                self.samples.append(sample)
                previous = sample
                self._stop.wait(max(1.0 - (time.monotonic() - started), 0.05))
        finally:
            engine.dispose()


def _pool_signals(response: httpx.Response) -> dict[str, float]:
    return {
        name: float(response.headers.get(header, "0"))
        for name, header in {
            "checked_out": "X-M8-Pool-Checked-Out",
            "overflow": "X-M8-Pool-Overflow",
            "size": "X-M8-Pool-Size",
            "capacity": "X-M8-Pool-Capacity",
        }.items()
    }


def _pool_summary(samples: list[dict[str, float]]) -> dict:
    if not samples:
        return {
            name: {"p50": 0.0, "p95": 0.0, "max": 0.0}
            for name in ("checked_out", "overflow", "size", "capacity")
        }
    return {
        name: {
            "p50": percentile([sample[name] for sample in samples], 0.50),
            "p95": percentile([sample[name] for sample in samples], 0.95),
            "max": max(sample[name] for sample in samples),
        }
        for name in ("checked_out", "overflow", "size", "capacity")
    }


def run_level(
    base_url: str,
    concurrency: int,
    duration_seconds: int,
    token: str,
    operation_id: str,
) -> dict:
    deadline = time.monotonic() + duration_seconds
    sequence = count()
    sequence_lock = Lock()
    result_lock = Lock()
    timings: dict[str, list[dict[str, float]]] = {}
    pool_samples: list[dict[str, float]] = []
    statuses: dict[int, int] = {}
    errors: list[dict] = []

    def worker() -> None:
        with httpx.Client(base_url=base_url, timeout=120) as client:
            while time.monotonic() < deadline:
                with sequence_lock:
                    current = next(sequence)
                method, path, kwargs, operation, expected = request_spec(
                    current, token, operation_id
                )
                try:
                    response, timing = measured_request(client, method, path, **kwargs)
                    with result_lock:
                        timings.setdefault(operation, []).append(timing)
                        pool_samples.append(_pool_signals(response))
                        statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
                        if response.status_code not in expected and len(errors) < 50:
                            errors.append(
                                {
                                    "operation": operation,
                                    "status": response.status_code,
                                    "body": response.text[:200],
                                }
                            )
                except httpx.HTTPError as error:
                    with result_lock:
                        statuses[0] = statuses.get(0, 0) + 1
                        if len(errors) < 50:
                            errors.append(
                                {"operation": operation, "error": type(error).__name__}
                            )

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(worker) for _ in range(concurrency)]
        for future in futures:
            future.result()
    elapsed = time.monotonic() - started
    all_timings = [sample for values in timings.values() for sample in values]
    overall = (
        summarize(all_timings)
        if all_timings
        else {
            name: {"p50": 0.0, "p95": 0.0}
            for name in (
                "client_wall_ms",
                "server_wall_ms",
                "connection_checkout_ms",
                "tenant_setup_sql_ms",
                "application_sql_ms",
                "application_processing_ms",
                "network_or_unaccounted_ms",
                "database_total_ms",
            )
        }
    )
    attempted = sum(statuses.values())
    successes = sum(
        count for status, count in statuses.items() if 200 <= status < 400
    )
    return {
        "concurrency": concurrency,
        "duration_seconds": round(elapsed, 3),
        "requests": attempted,
        "successful_requests": successes,
        "throughput_qps": attempted / elapsed,
        "successful_qps": successes / elapsed,
        "status_counts": {str(key): value for key, value in sorted(statuses.items())},
        "errors": errors,
        "overall": overall,
        "pool": _pool_summary(pool_samples),
        "by_operation": {
            operation: {"requests": len(values), **summarize(values)}
            for operation, values in sorted(timings.items())
        },
    }


def _read_errors(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def run(duration_seconds: int, base_url: str, output: Path) -> dict:
    pooled_url = os.environ.get("M8_DATABASE_URL_POOLED") or os.environ["DATABASE_URL"]
    direct_url = os.environ.get("M8_DATABASE_URL_UNPOOLED") or os.environ[
        "DATABASE_URL_UNPOOLED"
    ]
    token = f"m8-20-{uuid4()}"
    tenant_id = deterministic_uuid("m8-tenant-1")
    workspace_id = deterministic_uuid("m8-workspace-1")
    principal_id = deterministic_uuid("m8-principal-1")
    error_log = Path(f"/tmp/m8-20-errors-{os.getpid()}.jsonl")
    error_log.unlink(missing_ok=True)
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
        "M8_DIAGNOSTIC_ERROR_LOG": str(error_log),
    }
    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "evals.m8.capacity_instrumented_api:app",
            "--host",
            "127.0.0.1",
            "--port",
            base_url.rsplit(":", 1)[1],
            "--workers",
            "4",
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    worker = subprocess.Popen(
        [sys.executable, "-m", "memory_ops.worker"],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    current_concurrency = [0]
    sampler = Sampler(api.pid, worker.pid, current_concurrency)
    sampler_started = False
    try:
        with httpx.Client(base_url=base_url, timeout=120) as client:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("capacity API did not become ready")
            sampler.start()
            sampler_started = True
            core = core_flow(client, {"Authorization": f"Bearer {token}"})

        engine = _engine()
        try:
            with engine.connect() as connection:
                dataset = dict(
                    connection.execute(
                        text(
                            """
                            SELECT
                              (SELECT count(*) FROM user_memory_versions
                               WHERE policy_version = 'm8-fullscale-v1') AS canonical_memories,
                              (SELECT count(*) FROM knowledge_documents
                               WHERE source_id LIKE 'm8-fullscale-%') AS organizational_documents
                            """
                        )
                    ).mappings().one()
                )
        finally:
            engine.dispose()

        levels = []
        for concurrency in LEVELS:
            current_concurrency[0] = concurrency
            levels.append(
                run_level(
                    base_url,
                    concurrency,
                    duration_seconds,
                    token,
                    core["operation_id"],
                )
            )
        current_concurrency[0] = 0
        sampler.stop()
        sampler_started = False
        result = {
            "milestone": "m8-20",
            "status": "measured_candidate",
            "production_behavior_changed": False,
            "inputs": {path: digest(ROOT / path) for path in SOURCE_PATHS},
            "environment": {
                "branch_kind": "isolated_neon",
                "api_processes": 4,
                "worker_processes": 1,
                "host_cpu_count": os.cpu_count(),
                "seconds_per_level": duration_seconds,
                "concurrency_levels": list(LEVELS),
            },
            "dataset": {key: int(value) for key, value in dataset.items()},
            "core_flow": core,
            "levels": levels,
            "telemetry": {
                "sample_interval_seconds": 1,
                "samples": sampler.samples,
                "error_events": _read_errors(error_log),
            },
            "limits": [
                "All additional instrumentation is evaluation-only and records no request content.",
                "PostgreSQL activity and Neon consumption are capacity signals, not direct Neon host CPU telemetry.",
                "This diagnostic does not change production behavior or the production release decision.",
            ],
        }
        output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return result
    finally:
        if sampler_started:
            sampler.stop()
        stop_process(worker)
        stop_process(api)
        error_log.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds-per-level", type=int, default=30)
    parser.add_argument("--base-url", default="http://127.0.0.1:8767")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    run(args.seconds_per_level, args.base_url, args.output)


if __name__ == "__main__":
    main()

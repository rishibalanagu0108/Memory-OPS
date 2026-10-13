"""Measure M8 API latency components across a fixed concurrency sweep."""

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
from threading import Lock
import time
from uuid import uuid4

import httpx
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.m8.fullscale import _engine, deterministic_uuid, percentile, stop_process


OUTPUT = ROOT / "evals/m8/latency-attribution.json"
LEVELS = (1, 5, 10, 25, 50)
COMPONENTS = (
    "client_wall_ms",
    "server_wall_ms",
    "connection_checkout_ms",
    "tenant_setup_sql_ms",
    "application_sql_ms",
    "application_processing_ms",
    "network_or_unaccounted_ms",
    "database_total_ms",
)
SOURCE_PATHS = (
    "evals/m8/instrumented_api.py",
    "evals/m8/latency_attribution.py",
    "evals/m8/integrated-load-result.json",
)


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def timing_headers(response: httpx.Response, client_wall_ms: float) -> dict[str, float]:
    def value(name: str) -> float:
        key = f"X-M8-{name.replace('_', '-').title()}"
        return float(response.headers.get(key, "0"))

    server_wall_ms = value("server_wall_ms")
    result = {
        "client_wall_ms": client_wall_ms,
        "server_wall_ms": server_wall_ms,
        "connection_checkout_ms": value("connection_checkout_ms"),
        "tenant_setup_sql_ms": value("tenant_setup_sql_ms"),
        "application_sql_ms": value("application_sql_ms"),
        "application_processing_ms": value("application_processing_ms"),
        "network_or_unaccounted_ms": max(client_wall_ms - server_wall_ms, 0.0),
    }
    result["database_total_ms"] = sum(
        result[name]
        for name in (
            "connection_checkout_ms",
            "tenant_setup_sql_ms",
            "application_sql_ms",
        )
    )
    return result


def measured_request(
    client: httpx.Client,
    method: str,
    path: str,
    **kwargs,
) -> tuple[httpx.Response, dict[str, float]]:
    started = time.perf_counter()
    response = client.request(method, path, **kwargs)
    client_wall_ms = (time.perf_counter() - started) * 1000
    return response, timing_headers(response, client_wall_ms)


def core_flow(client: httpx.Client, headers: dict[str, str]) -> dict:
    tenant_id = deterministic_uuid("m8-tenant-1")
    workspace_id = deterministic_uuid("m8-workspace-1")
    subject_id = deterministic_uuid("m8-subject-1")
    root = f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}"
    samples: dict[str, dict[str, float]] = {}

    created, samples["remember"] = measured_request(
        client,
        "POST",
        f"{root}/memories",
        headers=headers | {"Idempotency-Key": f"m8-16-create-{uuid4()}"},
        json={
            "subject_id": str(subject_id),
            "semantic_type": "fact",
            "statement": "M8 latency attribution prefers measured architecture.",
            "purpose": "assistant_context",
            "evidence": [],
        },
    )
    created.raise_for_status()
    receipt = created.json()
    memory_path = f"{root}/memories/{receipt['memory_id']}"

    inspected, samples["retrieve"] = measured_request(
        client, "GET", memory_path, headers=headers
    )
    inspected.raise_for_status()
    if inspected.json()["statement"] != "M8 latency attribution prefers measured architecture.":
        raise RuntimeError("remembered content did not round trip")

    corrected, samples["correct"] = measured_request(
        client,
        "POST",
        f"{memory_path}/corrections",
        headers=headers | {"Idempotency-Key": f"m8-16-correct-{uuid4()}"},
        json={
            "subject_id": str(subject_id),
            "statement": "M8 latency attribution uses evidence before infrastructure.",
            "evidence": [],
        },
    )
    corrected.raise_for_status()

    context, samples["context"] = measured_request(
        client,
        "POST",
        f"{root}/context",
        headers=headers,
        json={
            "subject_id": str(subject_id),
            "query": "latency attribution evidence infrastructure",
            "purpose": "assistant_context",
            "token_budget": 256,
        },
    )
    context.raise_for_status()

    forgotten, samples["forget"] = measured_request(
        client,
        "DELETE",
        memory_path,
        headers=headers | {"Idempotency-Key": f"m8-16-forget-{uuid4()}"},
        params={"subject_id": str(subject_id)},
    )
    forgotten.raise_for_status()

    absent, samples["post_forget_retrieve"] = measured_request(
        client, "GET", memory_path, headers=headers
    )
    if absent.status_code != 404:
        raise RuntimeError("forgotten memory remained retrievable")
    return {
        "status": "pass",
        "memory_id": receipt["memory_id"],
        "operation_id": receipt["operation_id"],
        "post_forget_status": absent.status_code,
        "latency": samples,
    }


def request_spec(
    sequence: int,
    token: str,
    operation_id: str,
) -> tuple[str, str, dict, str, set[int]]:
    tenant_id = deterministic_uuid("m8-tenant-1")
    workspace_id = deterministic_uuid("m8-workspace-1")
    subject_id = deterministic_uuid("m8-subject-1")
    root = f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}"
    headers = {"Authorization": f"Bearer {token}"}
    operations = (
        "health",
        "operation_status",
        "memory_inspect",
        "context_build",
        "knowledge_search",
        "canonical_write",
    )
    operation = operations[sequence % len(operations)]
    if operation == "health":
        return "GET", "/health/ready", {}, operation, {200}
    if operation == "operation_status":
        return "GET", f"{root}/operations/{operation_id}", {"headers": headers}, operation, {200}
    if operation == "memory_inspect":
        memory_id = deterministic_uuid("m8-memory-1")
        return "GET", f"{root}/memories/{memory_id}", {"headers": headers}, operation, {200}
    if operation == "context_build":
        return (
            "POST",
            f"{root}/context",
            {
                "headers": headers,
                "json": {
                    "subject_id": str(subject_id),
                    "query": "memory profile",
                    "purpose": "assistant_context",
                    "token_budget": 256,
                },
            },
            operation,
            {200},
        )
    if operation == "knowledge_search":
        return (
            "POST",
            f"{root}/knowledge/search",
            {
                "headers": headers,
                "json": {"query": "organizational memory policy", "limit": 10},
            },
            operation,
            {200},
        )
    return (
        "POST",
        f"{root}/memories",
        {
            "headers": headers | {"Idempotency-Key": f"m8-16-load-{uuid4()}"},
            "json": {
                "subject_id": str(subject_id),
                "semantic_type": "fact",
                "statement": f"M8 latency sample {sequence} {uuid4()}",
                "purpose": "assistant_context",
            },
        },
        operation,
        {201},
    )


def summarize(samples: list[dict[str, float]]) -> dict:
    return {
        component: {
            "p50": percentile([sample[component] for sample in samples], 0.50),
            "p95": percentile([sample[component] for sample in samples], 0.95),
        }
        for component in COMPONENTS
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
    samples: dict[str, list[dict[str, float]]] = {}
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
                        samples.setdefault(operation, []).append(timing)
                        statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
                        if response.status_code not in expected and len(errors) < 20:
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
                        if len(errors) < 20:
                            errors.append(
                                {"operation": operation, "error": type(error).__name__}
                            )

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(worker) for _ in range(concurrency)]
        for future in futures:
            future.result()
    elapsed = time.monotonic() - started
    all_samples = [sample for values in samples.values() for sample in values]
    return {
        "concurrency": concurrency,
        "duration_seconds": round(elapsed, 3),
        "requests": len(all_samples),
        "throughput_qps": len(all_samples) / elapsed,
        "status_counts": {str(key): value for key, value in sorted(statuses.items())},
        "errors": errors,
        "overall": summarize(all_samples),
        "by_operation": {
            operation: {"requests": len(values), **summarize(values)}
            for operation, values in sorted(samples.items())
        },
    }


def findings(levels: list[dict]) -> list[dict]:
    single = levels[0]["overall"]
    loaded = levels[-1]["overall"]
    client_ratio = loaded["client_wall_ms"]["p95"] / max(
        single["client_wall_ms"]["p95"], 0.001
    )
    candidates = {
        name: single[name]["p95"]
        for name in (
            "database_total_ms",
            "application_processing_ms",
            "network_or_unaccounted_ms",
        )
    }
    dominant = max(candidates, key=candidates.get)
    return [
        {
            "cause": "single-client dominant latency component",
            "status": "supported",
            "component": dominant,
            "p95_ms": candidates[dominant],
        },
        {
            "cause": "concurrency-sensitive latency growth",
            "status": "supported" if client_ratio >= 2 else "not_observed",
            "p95_ratio_50_to_1": client_ratio,
        },
        {
            "cause": "Redis cache requirement",
            "status": "not_supported",
            "evidence": "the sweep does not establish a repeated-hot-read hit rate or a safe invalidation benefit",
        },
        {
            "cause": "graph database requirement",
            "status": "not_supported",
            "evidence": "the measured workload contains no multi-hop relationship query",
        },
    ]


def run(duration_seconds: int, base_url: str, output: Path) -> dict:
    pooled_url = os.environ.get("M8_DATABASE_URL_POOLED") or os.environ["DATABASE_URL"]
    direct_url = os.environ.get("M8_DATABASE_URL_UNPOOLED") or os.environ["DATABASE_URL_UNPOOLED"]
    token = f"m8-16-{uuid4()}"
    tenant_id = deterministic_uuid("m8-tenant-1")
    workspace_id = deterministic_uuid("m8-workspace-1")
    principal_id = deterministic_uuid("m8-principal-1")
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
        [
            sys.executable,
            "-m",
            "uvicorn",
            "evals.m8.instrumented_api:app",
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
                raise RuntimeError("instrumented API did not become ready")
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

        levels = [
            run_level(
                base_url,
                concurrency,
                duration_seconds,
                token,
                core["operation_id"],
            )
            for concurrency in LEVELS
        ]
        result = {
            "milestone": "m8-16",
            "status": "supported_diagnosis",
            "production_behavior_changed": False,
            "inputs": {path: digest(ROOT / path) for path in SOURCE_PATHS},
            "environment": {
                "branch_kind": "isolated_neon",
                "api_processes": 4,
                "worker_processes": 1,
                "seconds_per_level": duration_seconds,
                "concurrency_levels": list(LEVELS),
            },
            "dataset": dataset,
            "core_flow": core,
            "levels": levels,
            "findings": findings(levels),
            "limits": [
                "Latency headers exist only in the evaluation wrapper and contain no request content.",
                "Client wall time minus server wall time is network or harness overhead, not a pure network measurement.",
                "This run measures the current modular baseline and does not evaluate a Redis or graph counterfactual.",
                "The result does not change the production release decision.",
            ],
        }
        output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return result
    finally:
        stop_process(worker)
        stop_process(api)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds-per-level", type=int, default=30)
    parser.add_argument("--base-url", default="http://127.0.0.1:8766")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    run(args.seconds_per_level, args.base_url, args.output)


if __name__ == "__main__":
    main()

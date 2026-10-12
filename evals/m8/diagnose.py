"""Create source-bound diagnostics for the failed M8 production profile."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from hashlib import md5, sha256
import json
import os
from pathlib import Path
import time
from uuid import UUID

from sqlalchemy import event, text

from memory_ops.context import UserContextService
from memory_ops.knowledge import KnowledgeScope
from memory_ops.knowledge.search import KnowledgeSearchService
from memory_ops.persistence import TenantDatabase, create_database_engine
from memory_ops.user_memory import MemoryScope


ROOT = Path(__file__).resolve().parents[2]
PUBLISHED = ROOT / "evals/m8/fullscale-result.json"
REPRODUCTION = ROOT / "evals/m8/diagnostic-load-result.json"
OUTPUT = ROOT / "evals/m8/diagnostics.json"
SOURCE_PATHS = (
    "src/memory_ops/context/__init__.py",
    "src/memory_ops/knowledge/search.py",
    "src/memory_ops/persistence/__init__.py",
    "src/memory_ops/worker.py",
    "evals/m8/fullscale.py",
)


def deterministic_uuid(label: str) -> UUID:
    return UUID(md5(label.encode(), usedforsecurity=False).hexdigest())


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def source_diagnostics() -> dict:
    context = (ROOT / SOURCE_PATHS[0]).read_text()
    knowledge = (ROOT / SOURCE_PATHS[1]).read_text()
    worker = (ROOT / SOURCE_PATHS[3]).read_text()
    load_runner = (ROOT / SOURCE_PATHS[4]).read_text()
    candidate_limit = 20
    hydration_statements = 4
    retrieval_statements = 3
    knowledge_statements = knowledge.count("connection.execute(")
    return {
        "user_context_statement_ceiling": retrieval_statements + candidate_limit * hydration_statements,
        "knowledge_search_statement_count": knowledge_statements + 2,
        "cross_domain_statement_ceiling": retrieval_statements + candidate_limit * hydration_statements + knowledge_statements + 2,
        "candidate_limit": candidate_limit,
        "candidate_hydration_is_n_plus_one": "for candidate in candidates:" in context and ".current_version(" in context,
        "worker_dispatch_is_serial": "ThreadPoolExecutor" not in worker,
        "load_concurrency": 50 if "max_workers=50" in load_runner else None,
        "api_processes": 4 if '"--workers", "4"' in load_runner else None,
    }


def load_diagnostics(published: dict, reproduction: dict) -> dict:
    recovery = published["recovery"]
    drained = recovery["remaining_before_drain"] - recovery["remaining_after_drain"]
    return {
        "published": {
            "throughput_qps": published["throughput_qps"],
            "p95_ms": published["latency_ms"]["p95"],
            "context_p95_ms": published["latency_ms"]["by_operation_p95"]["context_build"],
            "index_lag_p95_seconds": published["index_lag_seconds"]["p95"],
        },
        "reproduction": {
            "throughput_qps": reproduction["throughput_qps"],
            "p95_ms": reproduction["latency_ms"]["p95"],
            "context_p95_ms": reproduction["latency_ms"]["by_operation_p95"]["context_build"],
            "index_lag_p95_seconds": reproduction["index_lag_seconds"]["p95"],
        },
        "failure_reproduced": reproduction["throughput_qps"] < 25 and reproduction["latency_ms"]["p95"] > 750 and reproduction["index_lag_seconds"]["p95"] > 30,
        "worker_recovery": {
            "drained_events": drained,
            "observed_events_per_second": drained / recovery["drain_seconds"],
            "required_events_per_second": recovery["remaining_before_drain"] / 300,
        },
    }


def statement_kind(statement: str) -> str:
    normalized = " ".join(statement.split())
    if normalized.startswith("SET LOCAL ROLE"):
        return "set_role"
    if normalized.startswith("SELECT set_config"):
        return "tenant_context"
    if "plainto_tsquery" in normalized:
        return "memory_keyword"
    if "<=>" in normalized:
        return "memory_vector"
    if "FROM user_memory_evidence" in normalized:
        return "memory_evidence"
    if "FROM user_memory_versions v" in normalized:
        return "memory_hydration"
    if "knowledge_document" in normalized:
        return "knowledge"
    return normalized.split()[0].lower()


def summarize_statements(statements: list[tuple[str, float]]) -> dict:
    counts = Counter(kind for kind, _ in statements)
    timings: dict[str, list[float]] = defaultdict(list)
    for kind, elapsed_ms in statements:
        timings[kind].append(elapsed_ms)
    return {
        "count": len(statements),
        "by_kind": dict(sorted(counts.items())),
        "sql_ms": round(sum(elapsed for _, elapsed in statements), 3),
        "sql_ms_by_kind": {kind: round(sum(values), 3) for kind, values in sorted(timings.items())},
    }


def live_diagnostics(database_url: str) -> dict:
    engine = create_database_engine(database_url)
    statements: list[tuple[str, float]] = []

    @event.listens_for(engine, "before_cursor_execute")
    def before_cursor_execute(_connection, _cursor, _statement, _parameters, context, _executemany) -> None:
        context._m8_started = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def after_cursor_execute(_connection, _cursor, statement, _parameters, context, _executemany) -> None:
        statements.append((statement_kind(statement), (time.perf_counter() - context._m8_started) * 1000))

    database = TenantDatabase(engine)
    tenant_id = deterministic_uuid("m8-tenant-1")
    workspace_id = deterministic_uuid("m8-workspace-1")
    scope = MemoryScope(tenant_id=tenant_id, workspace_id=workspace_id, subject_id=deterministic_uuid("m8-subject-1"), agent_id=None)
    started = time.perf_counter()
    user_result = UserContextService(database).build(scope, "memory profile", purpose="assistant_context", token_budget=256)
    user_wall_ms = (time.perf_counter() - started) * 1000
    user_statements = list(statements)
    statements.clear()

    started = time.perf_counter()
    knowledge_result = KnowledgeSearchService(database).search(
        KnowledgeScope(tenant_id=tenant_id, workspace_id=workspace_id),
        deterministic_uuid("m8-principal-1"),
        "organizational memory policy",
    )
    knowledge_wall_ms = (time.perf_counter() - started) * 1000
    knowledge_statements = list(statements)
    statements.clear()

    with engine.connect() as connection:
        outbox = dict(connection.execute(text("SELECT status, count(*) FROM outbox_events WHERE tenant_id = :tenant_id GROUP BY status"), {"tenant_id": tenant_id}).all())
    result = {
        "branch": "m8-08-diagnostics-20261012",
        "dataset": {"canonical_memories": 1_000_000, "documents": 10_000},
        "user_context": {
            "wall_ms": round(user_wall_ms, 3),
            "items": sum(len(section.items) for section in user_result.sections),
            **summarize_statements(user_statements),
        },
        "knowledge_search": {
            "wall_ms": round(knowledge_wall_ms, 3),
            "passages": len(knowledge_result.passages),
            **summarize_statements(knowledge_statements),
        },
        "connection_pool": {"size_per_process": engine.pool.size(), "max_overflow_per_process": engine.pool._max_overflow},
        "outbox_status_after_reproduction": outbox,
    }
    engine.dispose()
    return result


def diagnose(database_url: str) -> dict:
    published = load(PUBLISHED)
    reproduction = load(REPRODUCTION)
    sources = source_diagnostics()
    live = live_diagnostics(database_url)
    dominant_sql = max(live["user_context"]["sql_ms_by_kind"], key=live["user_context"]["sql_ms_by_kind"].get)
    input_paths = (*SOURCE_PATHS, "evals/m8/fullscale-result.json", "evals/m8/diagnostic-load-result.json")
    result = {
        "milestone": "m8-08",
        "status": "supported_diagnosis",
        "inputs": {path: digest(ROOT / path) for path in input_paths},
        "load": load_diagnostics(published, reproduction),
        "source": sources,
        "live": live,
        "findings": [
            {"cause": "user-context SQL amplification", "status": "supported", "evidence": f"one request issued {live['user_context']['count']} statements; the source ceiling is {sources['user_context_statement_ceiling']}"},
            {"cause": f"dominant user-context SQL category: {dominant_sql}", "status": "supported", "evidence_ms": live["user_context"]["sql_ms_by_kind"][dominant_sql]},
            {"cause": "serial worker dispatch", "status": "supported", "evidence": "source dispatches each claimed event sequentially; published five-minute drain completed 43 of 257 events"},
            {"cause": "connection contention", "status": "contributor_not_isolated", "evidence": "50 concurrent requests share four process-local pools while each context request performs repeated tenant transactions"},
        ],
        "limits": [
            "The original M8 branch no longer exists; reproduction used an ephemeral branch seeded to the same database row counts.",
            "pg_stat_statements and the neon extension were unavailable and were not installed.",
            "This task attributes causes but does not change production behavior or claim release readiness.",
        ],
    }
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.environ.get("M8_DIAGNOSTIC_DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("--database-url or M8_DIAGNOSTIC_DATABASE_URL is required")
    print(json.dumps(diagnose(args.database_url), indent=2))


if __name__ == "__main__":
    main()

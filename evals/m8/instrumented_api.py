"""Evaluation-only API wrapper that exposes content-free latency components."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import time
from typing import Iterator
from uuid import UUID

from fastapi import Request
from sqlalchemy import event, text
from sqlalchemy.engine import Connection

from memory_ops.api import create_app
from memory_ops.config import get_settings
from memory_ops.persistence import TenantDatabase, create_database_engine


_timing: ContextVar[dict[str, float] | None] = ContextVar("m8_timing", default=None)
engine = create_database_engine(get_settings().database_url)


def _add(name: str, elapsed_ms: float) -> None:
    timing = _timing.get()
    if timing is not None:
        timing[name] += elapsed_ms


@event.listens_for(engine, "before_cursor_execute")
def _before_cursor_execute(
    _connection, _cursor, _statement, _parameters, context, _executemany
) -> None:
    context._m8_started = time.perf_counter()


@event.listens_for(engine, "after_cursor_execute")
def _after_cursor_execute(
    _connection, _cursor, statement, _parameters, context, _executemany
) -> None:
    elapsed_ms = (time.perf_counter() - context._m8_started) * 1000
    normalized = " ".join(statement.split())
    category = (
        "tenant_setup_sql_ms"
        if normalized.startswith("SET LOCAL ROLE")
        or normalized.startswith("SELECT set_config")
        else "application_sql_ms"
    )
    _add(category, elapsed_ms)


class InstrumentedTenantDatabase(TenantDatabase):
    @contextmanager
    def transaction(self, tenant_id: UUID) -> Iterator[Connection]:
        started = time.perf_counter()
        with self.engine.begin() as connection:
            _add("connection_checkout_ms", (time.perf_counter() - started) * 1000)
            connection.execute(text("SET LOCAL ROLE memory_ops_runtime"))
            connection.execute(
                text("SELECT set_config('memory_ops.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            yield connection


app = create_app(database=InstrumentedTenantDatabase(engine))


@app.middleware("http")
async def expose_latency_components(request: Request, call_next):
    timing = {
        "connection_checkout_ms": 0.0,
        "tenant_setup_sql_ms": 0.0,
        "application_sql_ms": 0.0,
    }
    token = _timing.set(timing)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        server_wall_ms = (time.perf_counter() - started) * 1000
        accounted = sum(timing.values())
        values = timing | {
            "server_wall_ms": server_wall_ms,
            "application_processing_ms": max(server_wall_ms - accounted, 0.0),
        }
        for name, value in values.items():
            response.headers[f"X-M8-{name.replace('_', '-').title()}"] = f"{value:.6f}"
        return response
    finally:
        _timing.reset(token)

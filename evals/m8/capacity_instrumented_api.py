"""Evaluation-only pool and error signals for the M8 capacity run."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import event

from evals.m8.instrumented_api import app, engine


ERROR_LOG = os.environ.get("M8_DIAGNOSTIC_ERROR_LOG")


def _record(kind: str, error: BaseException) -> None:
    if not ERROR_LOG:
        return
    payload = {
        "at_epoch_seconds": time.time(),
        "pid": os.getpid(),
        "kind": kind,
        "error_type": type(error).__name__,
    }
    descriptor = os.open(ERROR_LOG, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, (json.dumps(payload, sort_keys=True) + "\n").encode())
    finally:
        os.close(descriptor)


def _pool_value(name: str) -> float:
    value = getattr(engine.pool, name, None)
    return float(value() if callable(value) else 0.0)


@event.listens_for(engine, "handle_error")
def _record_database_error(context) -> None:
    _record("database", context.original_exception)


@app.middleware("http")
async def expose_capacity_signals(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-M8-Pool-Checked-Out"] = str(_pool_value("checkedout"))
    response.headers["X-M8-Pool-Overflow"] = str(_pool_value("overflow"))
    response.headers["X-M8-Pool-Size"] = str(_pool_value("size"))
    max_overflow = float(getattr(engine.pool, "_max_overflow", 0.0))
    response.headers["X-M8-Pool-Capacity"] = str(
        _pool_value("size") + max(max_overflow, 0.0)
    )
    return response


@app.exception_handler(Exception)
async def record_internal_error(_: Request, error: Exception) -> JSONResponse:
    _record("application", error)
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error", "message": "internal error"},
    )


Path(ERROR_LOG).touch(mode=0o600, exist_ok=True) if ERROR_LOG else None

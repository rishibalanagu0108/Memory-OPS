"""Versioned HTTP API and OpenAPI contract."""

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from memory_ops import __version__
from memory_ops.agent_learning import LessonPromotionRegistry
from memory_ops.api.agent_learning import install_agent_learning_routes
from memory_ops.api.openapi import install_shared_schemas
from memory_ops.api.context import install_context_routes
from memory_ops.api.knowledge import install_knowledge_routes
from memory_ops.api.security import configured_security_boundary
from memory_ops.api.user_memory import install_user_memory_routes
from memory_ops.config import Settings, get_settings
from memory_ops.persistence import (
    TenantDatabase,
    create_database_engine,
)
from memory_ops.persistence.writes import IdempotencyConflict
from memory_ops.operations import RuntimeGuard
from memory_ops.security import SecurityBoundary, SecurityError


class Health(BaseModel):
    status: Literal["alive", "ready"]
    service: str
    environment: str


def create_app(
    settings: Settings | None = None,
    security: SecurityBoundary | None = None,
    database: TenantDatabase | None = None,
    lesson_promotions: LessonPromotionRegistry | None = None,
) -> FastAPI:
    configured = settings or get_settings()
    boundary = security or configured_security_boundary(configured)
    tenant_database = database or TenantDatabase(
        create_database_engine(configured.database_url)
    )
    owns_database = database is None
    runtime = RuntimeGuard(
        tenant_database,
        boundary,
        configured.api_max_in_flight,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        runtime.start_draining()
        if owns_database:
            tenant_database.engine.dispose()

    app = FastAPI(
        title=configured.service_name,
        version=__version__,
        openapi_url="/v1/openapi.json",
        lifespan=lifespan,
    )
    app.state.security = boundary
    app.state.database = tenant_database
    app.state.lesson_promotions = lesson_promotions or LessonPromotionRegistry(
        tenant_database, current_source_hash=None
    )
    app.state.runtime = runtime

    @app.middleware("http")
    async def enforce_runtime_quota(request: Request, call_next):
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)
        admission = runtime.admit()
        if admission == "draining":
            return JSONResponse(
                status_code=503,
                content={"code": "service_unavailable", "message": "service unavailable"},
            )
        if admission == "quota":
            return JSONResponse(
                status_code=429,
                content={"code": "quota_exceeded", "message": "request quota exceeded"},
                headers={"Retry-After": "1"},
            )
        try:
            return await call_next(request)
        finally:
            runtime.release()

    @app.exception_handler(SecurityError)
    async def security_error(_: Request, error: SecurityError) -> JSONResponse:
        status_code = {"unauthenticated": 401, "invalid_request": 400}.get(
            error.code, 403
        )
        headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
        return JSONResponse(
            status_code=status_code,
            content={"code": error.code, "message": error.message},
            headers=headers,
        )

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict(_: Request, __: IdempotencyConflict) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"code": "conflict", "message": "idempotency key conflict"},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"code": "invalid_request", "message": "invalid request"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, error: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if error.status_code == 404 else "invalid_request"
        message = "resource not found" if error.status_code == 404 else "invalid request"
        return JSONResponse(
            status_code=error.status_code,
            content={"code": code, "message": message},
        )

    @app.exception_handler(Exception)
    async def internal_error(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "internal error"},
        )

    @app.get("/health/live", response_model=Health, tags=["health"])
    def live() -> Health:
        return Health(
            status="alive",
            service=configured.service_name,
            environment=configured.environment,
        )

    @app.get(
        "/health/ready",
        response_model=Health,
        tags=["health"],
    )
    def ready() -> Health | JSONResponse:
        if not runtime.ready():
            return JSONResponse(
                status_code=503,
                content={"code": "service_unavailable", "message": "service unavailable"},
            )
        return Health(
            status="ready",
            service=configured.service_name,
            environment=configured.environment,
        )

    install_user_memory_routes(app)
    install_knowledge_routes(app)
    install_context_routes(app)
    install_agent_learning_routes(app)
    install_shared_schemas(app)
    return app


app = create_app()


def run() -> None:
    import uvicorn

    configured = get_settings()
    uvicorn.run(
        "memory_ops.api:app",
        host="0.0.0.0",
        port=8000,
        limit_concurrency=configured.api_max_in_flight,
        timeout_graceful_shutdown=configured.api_graceful_shutdown_seconds,
    )

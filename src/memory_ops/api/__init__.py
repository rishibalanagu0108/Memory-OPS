"""Versioned HTTP API and OpenAPI contract."""

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
from memory_ops.api.security import configured_security_boundary
from memory_ops.api.user_memory import install_user_memory_routes
from memory_ops.config import Settings, get_settings
from memory_ops.persistence import (
    TenantDatabase,
    create_database_engine,
)
from memory_ops.persistence.writes import IdempotencyConflict
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
    app = FastAPI(
        title=configured.service_name,
        version=__version__,
        openapi_url="/v1/openapi.json",
    )
    app.state.security = boundary
    app.state.database = tenant_database
    app.state.lesson_promotions = lesson_promotions or LessonPromotionRegistry(
        tenant_database, current_source_hash=None
    )

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

    @app.get("/health/ready", response_model=Health, tags=["health"])
    def ready() -> Health:
        return Health(
            status="ready",
            service=configured.service_name,
            environment=configured.environment,
        )

    install_user_memory_routes(app)
    install_context_routes(app)
    install_agent_learning_routes(app)
    install_shared_schemas(app)
    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("memory_ops.api:app", host="0.0.0.0", port=8000)

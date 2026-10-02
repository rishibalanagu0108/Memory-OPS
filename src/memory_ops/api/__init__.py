"""Versioned HTTP API and OpenAPI contract."""

from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memory_ops import __version__
from memory_ops.api.openapi import install_shared_schemas
from memory_ops.config import Settings, get_settings
from memory_ops.security import SecurityBoundary, SecurityError


class Health(BaseModel):
    status: Literal["alive", "ready"]
    service: str
    environment: str


def create_app(
    settings: Settings | None = None,
    security: SecurityBoundary | None = None,
) -> FastAPI:
    configured = settings or get_settings()
    app = FastAPI(
        title=configured.service_name,
        version=__version__,
        openapi_url="/v1/openapi.json",
    )
    app.state.security = security or SecurityBoundary()

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

    install_shared_schemas(app)
    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("memory_ops.api:app", host="0.0.0.0", port=8000)

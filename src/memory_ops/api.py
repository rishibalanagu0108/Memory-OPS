"""HTTP service bootstrap."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from memory_ops import __version__
from memory_ops.config import Settings, get_settings


class Health(BaseModel):
    status: Literal["alive", "ready"]
    service: str
    environment: str


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or get_settings()
    app = FastAPI(title=configured.service_name, version=__version__)

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

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("memory_ops.api:app", host="0.0.0.0", port=8000)


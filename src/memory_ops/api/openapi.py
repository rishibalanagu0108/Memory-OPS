"""OpenAPI schema registration."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from memory_ops.api.schemas import SHARED_SCHEMAS


def install_shared_schemas(app: FastAPI) -> None:
    build_openapi: Callable[[], dict[str, Any]] = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            document = build_openapi()
            schemas = document.setdefault("components", {}).setdefault("schemas", {})
            schemas.update(
                {
                    model.__name__: model.model_json_schema(
                        ref_template="#/components/schemas/{model}"
                    )
                    for model in SHARED_SCHEMAS
                }
            )
            security_schemes = document["components"].setdefault(
                "securitySchemes", {}
            )
            security_schemes["BearerAuth"] = {
                "type": "http",
                "scheme": "bearer",
                "description": "Enter the API token without the 'Bearer ' prefix.",
            }
            for path, path_item in document.get("paths", {}).items():
                if not path.startswith("/v1/"):
                    continue
                for operation in path_item.values():
                    if isinstance(operation, dict) and "responses" in operation:
                        operation["security"] = [{"BearerAuth": []}]
            app.openapi_schema = document
        return app.openapi_schema

    app.openapi = openapi

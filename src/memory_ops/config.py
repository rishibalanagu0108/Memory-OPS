"""Validated process configuration."""

import os
from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, PostgresDsn, SecretStr, model_validator


class Settings(BaseModel):
    """Configuration shared by API and worker processes."""

    model_config = ConfigDict(frozen=True)

    service_name: str = "memory-ops"
    environment: Literal["development", "test", "production"] = "development"
    database_url: PostgresDsn
    migration_database_url: PostgresDsn
    api_token: SecretStr | None = None
    api_tenant_id: UUID | None = None
    api_workspace_id: UUID | None = None
    api_principal_id: UUID | None = None

    @model_validator(mode="after")
    def validate_api_identity(self) -> "Settings":
        identity = (
            self.api_tenant_id,
            self.api_workspace_id,
            self.api_principal_id,
        )
        if self.api_token is not None and not all(identity):
            raise ValueError("API token requires tenant, workspace, and principal IDs")
        if self.api_token is None and any(identity):
            raise ValueError("API identity requires an API token")
        return self

    @classmethod
    def from_environment(cls) -> "Settings":
        values = {
            "service_name": "MEMORY_OPS_SERVICE_NAME",
            "environment": "MEMORY_OPS_ENVIRONMENT",
            "api_token": "MEMORY_OPS_API_TOKEN",
            "api_tenant_id": "MEMORY_OPS_API_TENANT_ID",
            "api_workspace_id": "MEMORY_OPS_API_WORKSPACE_ID",
            "api_principal_id": "MEMORY_OPS_API_PRINCIPAL_ID",
        }
        configured = {
            field: os.environ[name]
            for field, name in values.items()
            if name in os.environ
        }
        runtime_url = os.getenv("MEMORY_OPS_DATABASE_URL") or os.getenv("DATABASE_URL")
        migration_url = (
            os.getenv("MEMORY_OPS_MIGRATION_DATABASE_URL")
            or os.getenv("DATABASE_URL_UNPOOLED")
            or runtime_url
        )
        if runtime_url:
            configured["database_url"] = runtime_url
        if migration_url:
            configured["migration_database_url"] = migration_url
        return cls.model_validate(configured)


@lru_cache
def get_settings() -> Settings:
    return Settings.from_environment()

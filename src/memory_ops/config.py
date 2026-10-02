"""Validated process configuration."""

import os
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, PostgresDsn


_LOCAL_DATABASE_URL = "postgresql://memory_ops:memory_ops@localhost:5432/memory_ops"


class Settings(BaseModel):
    """Configuration shared by API and worker processes."""

    model_config = ConfigDict(frozen=True)

    service_name: str = "memory-ops"
    environment: Literal["development", "test", "production"] = "development"
    database_url: PostgresDsn = PostgresDsn(_LOCAL_DATABASE_URL)
    migration_database_url: PostgresDsn = PostgresDsn(_LOCAL_DATABASE_URL)

    @classmethod
    def from_environment(cls) -> "Settings":
        values = {
            "service_name": "MEMORY_OPS_SERVICE_NAME",
            "environment": "MEMORY_OPS_ENVIRONMENT",
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

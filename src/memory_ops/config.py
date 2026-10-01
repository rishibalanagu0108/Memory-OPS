"""Validated process configuration."""

import os
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, PostgresDsn


class Settings(BaseModel):
    """Configuration shared by API and worker processes."""

    model_config = ConfigDict(frozen=True)

    service_name: str = "memory-ops"
    environment: Literal["development", "test", "production"] = "development"
    database_url: PostgresDsn = PostgresDsn(
        "postgresql://memory_ops:memory_ops@localhost:5432/memory_ops"
    )

    @classmethod
    def from_environment(cls) -> "Settings":
        names = {
            "service_name": "MEMORY_OPS_SERVICE_NAME",
            "environment": "MEMORY_OPS_ENVIRONMENT",
            "database_url": "MEMORY_OPS_DATABASE_URL",
        }
        return cls.model_validate(
            {field: os.environ[name] for field, name in names.items() if name in os.environ}
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_environment()


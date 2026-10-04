from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from memory_ops.api import create_app
from memory_ops.config import Settings


def test_health_endpoints_report_configured_service() -> None:
    settings = Settings.from_environment().model_copy(
        update={"service_name": "memory-ops-test", "environment": "test"}
    )
    client = TestClient(
        create_app(settings)
    )

    assert client.get("/health/live").json() == {
        "status": "alive",
        "service": "memory-ops-test",
        "environment": "test",
    }
    assert client.get("/health/ready").json() == {
        "status": "ready",
        "service": "memory-ops-test",
        "environment": "test",
    }


def test_settings_read_prefixed_environment(monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_OPS_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "MEMORY_OPS_DATABASE_URL",
        "postgresql://service:secret@database.example/memory_ops",
    )
    monkeypatch.setenv(
        "MEMORY_OPS_MIGRATION_DATABASE_URL",
        "postgresql://owner:secret@direct.example/memory_ops",
    )

    settings = Settings.from_environment()

    assert settings.environment == "production"
    assert "@database.example/" in str(settings.database_url)
    assert "@direct.example/" in str(settings.migration_database_url)


def test_settings_read_neon_environment(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_OPS_DATABASE_URL", raising=False)
    monkeypatch.delenv("MEMORY_OPS_MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://service:secret@pooled.example/memory_ops",
    )
    monkeypatch.setenv(
        "DATABASE_URL_UNPOOLED",
        "postgresql://owner:secret@direct.example/memory_ops",
    )

    settings = Settings.from_environment()

    assert "@pooled.example/" in str(settings.database_url)
    assert "@direct.example/" in str(settings.migration_database_url)


def test_database_configuration_is_required(monkeypatch) -> None:
    for name in (
        "MEMORY_OPS_DATABASE_URL",
        "MEMORY_OPS_MIGRATION_DATABASE_URL",
        "DATABASE_URL",
        "DATABASE_URL_UNPOOLED",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        Settings.from_environment()

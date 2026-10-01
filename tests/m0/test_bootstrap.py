from pathlib import Path

from fastapi.testclient import TestClient

from memory_ops.api import create_app
from memory_ops.config import Settings


def test_health_endpoints_report_configured_service() -> None:
    client = TestClient(
        create_app(Settings(service_name="memory-ops-test", environment="test"))
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

    settings = Settings.from_environment()

    assert settings.environment == "production"
    assert "@database.example/" in str(settings.database_url)


def test_compose_defines_pinned_healthy_postgres() -> None:
    compose = Path("docker-compose.yml").read_text()

    assert "image: postgres:17.6-alpine" in compose
    assert "pg_isready -U memory_ops -d memory_ops" in compose
    assert "postgres-data:/var/lib/postgresql/data" in compose

from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from memory_ops.api import create_app
from memory_ops.config import Settings
from memory_ops.operations import RuntimeGuard
from memory_ops.persistence import TenantDatabase
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    SecurityBoundary,
    WorkspaceGrant,
)
from memory_ops.worker import run_until_stopped
from memory_ops.workers import OutboxEvent


def settings(**updates) -> Settings:
    return Settings(
        database_url="postgresql://user:pass@localhost/runtime",
        migration_database_url="postgresql://user:pass@localhost/migrations",
        environment="test",
        **updates,
    )


def security() -> SecurityBoundary:
    tenant_id, workspace_id = uuid4(), uuid4()
    principal = AuthenticatedPrincipal(
        tenant_id,
        uuid4(),
        (WorkspaceGrant(workspace_id, frozenset({"memory:read"})),),
    )
    return SecurityBoundary(
        credential_resolver=lambda credential: principal if credential == "valid" else None,
        policy=MachinePolicy("production-policy-v1", frozenset({"memory:read"})),
    )


def test_readiness_checks_policy_database_and_drain_state() -> None:
    engine = create_engine("sqlite://")
    database = TenantDatabase(engine)
    app = create_app(settings(), security(), database)

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
    assert app.state.runtime.ready() is False
    engine.dispose()

    unavailable = create_app(settings(), SecurityBoundary(), database)
    with TestClient(unavailable) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {
            "code": "service_unavailable",
            "message": "service unavailable",
        }


def test_runtime_quota_and_drain_reject_new_work() -> None:
    engine = create_engine("sqlite://")
    guard = RuntimeGuard(TenantDatabase(engine), security(), max_in_flight=1)

    assert guard.admit() == "admitted"
    assert guard.admit() == "quota"
    guard.release()
    guard.start_draining()
    assert guard.admit() == "draining"
    assert guard.ready() is False
    engine.dispose()

    engine = create_engine("sqlite://")
    app = create_app(
        settings(api_max_in_flight=1), security(), TenantDatabase(engine)
    )
    with TestClient(app) as client:
        assert app.state.runtime.admit() == "admitted"
        limited = client.get("/v1/openapi.json")
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "1"
        app.state.runtime.release()
        app.state.runtime.start_draining()
        assert client.get("/v1/openapi.json").status_code == 503
    engine.dispose()


def test_policy_adapter_failure_denies_before_data_access() -> None:
    engine = create_engine("sqlite://")

    def unavailable_identity(_: str):
        raise RuntimeError("identity provider unavailable")

    boundary = SecurityBoundary(
        credential_resolver=unavailable_identity,
        policy=MachinePolicy("production-policy-v1", frozenset({"memory:read"})),
    )
    app = create_app(settings(), boundary, TenantDatabase(engine))
    with TestClient(app) as client:
        response = client.get(
            f"/v1/tenants/{uuid4()}/workspaces/{uuid4()}/memories",
            headers={"Authorization": "Bearer valid"},
        )
        assert response.status_code == 401
        assert response.json() == {
            "code": "unauthenticated",
            "message": "authentication required",
        }
    engine.dispose()


def test_worker_finishes_claimed_batch_before_stopping() -> None:
    stopped = Event()
    event = OutboxEvent(
        uuid4(),
        "user_memory.version.created",
        "user_memory",
        uuid4(),
        uuid4(),
        1,
    )

    class Outbox:
        claims = 0

        def claim(self, _tenant_id, limit):
            self.claims += 1
            assert limit == 1
            return [event]

        def retry(self, *_args, **_kwargs):
            raise AssertionError("successful work must not retry")

    class Embeddings:
        def process(self, _tenant_id, claimed):
            assert claimed == event
            stopped.set()

    outbox = Outbox()
    run_until_stopped(
        uuid4(),
        outbox,
        Embeddings(),
        SimpleNamespace(process=lambda *_: None),
        stopped,
        batch_size=1,
        idle_seconds=0.01,
    )
    assert outbox.claims == 1

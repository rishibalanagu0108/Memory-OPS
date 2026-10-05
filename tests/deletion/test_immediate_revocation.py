from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from memory_ops.api import create_app
from memory_ops.config import Settings
from memory_ops.lifecycle import LifecycleService
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    SecurityBoundary,
    WorkspaceGrant,
)
from memory_ops.user_memory import (
    CorrectionRequest,
    MemoryScope,
    RememberRequest,
    UserMemoryService,
)


@pytest.fixture(scope="module")
def lifecycle_store() -> tuple[TestClient, Engine, LifecycleService, MemoryScope]:
    settings = Settings.from_environment()
    engine = create_database_engine(settings.database_url)
    upgrade_database(engine)
    tenant_id, workspace_id, subject_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants (id) VALUES (:id)"), {"id": tenant_id})
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant_id)"),
            {"id": workspace_id, "tenant_id": tenant_id},
        )
    scope = MemoryScope(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        subject_id=subject_id,
    )
    principal = AuthenticatedPrincipal(
        tenant_id=tenant_id,
        principal_id=uuid4(),
        workspace_grants=(
            WorkspaceGrant(
                workspace_id,
                frozenset({"memory:read", "memory:write"}),
            ),
        ),
    )
    security = SecurityBoundary(
        credential_resolver=lambda credential: principal if credential == "valid" else None,
        policy=MachinePolicy(
            "policy-2026-10",
            frozenset({"memory:read", "memory:write"}),
        ),
    )
    app = create_app(
        settings.model_copy(update={"environment": "test"}),
        security,
        TenantDatabase(engine),
    )
    with TestClient(app) as client:
        yield client, engine, LifecycleService(TenantDatabase(engine)), scope
    engine.dispose()


def remember(
    engine: Engine,
    scope: MemoryScope,
    *,
    retention_until: datetime | None = None,
) -> tuple[UUID, UUID]:
    result = UserMemoryService(TenantDatabase(engine), "policy-2026-10").remember(
        RememberRequest(
            scope=scope,
            semantic_type="preference",
            statement="The user prefers concise answers.",
            purpose="assistant_context",
            retention_until=retention_until,
        ),
        f"remember-{uuid4()}",
    )
    return result.receipt.resource_id, result.receipt.resource_version


def memory_path(scope: MemoryScope, memory_id: UUID) -> str:
    return f"/v1/tenants/{scope.tenant_id}/workspaces/{scope.workspace_id}/memories/{memory_id}"


def test_authorized_forget_revokes_retrieval_and_queues_versioned_purge(
    lifecycle_store: tuple[TestClient, Engine, LifecycleService, MemoryScope],
) -> None:
    client, engine, lifecycle, scope = lifecycle_store
    memory_id, version_id = remember(engine, scope)

    response = client.delete(
        memory_path(scope, memory_id),
        params={"subject_id": str(scope.subject_id)},
        headers={"Authorization": "Bearer valid", "Idempotency-Key": f"forget-{uuid4()}"},
    )

    assert response.status_code == 202
    assert response.json()["version_id"] == str(version_id)
    assert client.get(
        memory_path(scope, memory_id), headers={"Authorization": "Bearer valid"}
    ).status_code == 404
    assert lifecycle.is_retrievable(scope.tenant_id, memory_id, version_id) is False
    with TenantDatabase(engine).transaction(scope.tenant_id) as connection:
        state = connection.execute(
            text("SELECT lifecycle FROM user_memories WHERE id = :id"),
            {"id": memory_id},
        ).scalar_one()
        event = connection.execute(
            text(
                "SELECT event_type, resource_version, status FROM outbox_events "
                "WHERE id = :id"
            ),
            {"id": UUID(response.json()["operation_id"])},
        ).one()
    assert state == "revoked"
    assert tuple(event) == ("user_memory.purge.requested", version_id, "pending")


def test_expired_memory_is_filtered_before_cleanup_and_queues_purge_once(
    lifecycle_store: tuple[TestClient, Engine, LifecycleService, MemoryScope],
) -> None:
    client, engine, lifecycle, scope = lifecycle_store
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    memory_id, version_id = remember(engine, scope, retention_until=expired_at)

    assert client.get(
        memory_path(scope, memory_id), headers={"Authorization": "Bearer valid"}
    ).status_code == 404
    assert lifecycle.is_retrievable(scope.tenant_id, memory_id, version_id) is False

    results = lifecycle.expire_due(scope.tenant_id)
    assert [(item.memory_id, item.version_id) for item in results] == [(memory_id, version_id)]
    assert lifecycle.expire_due(scope.tenant_id) == ()
    with TenantDatabase(engine).transaction(scope.tenant_id) as connection:
        state = connection.execute(
            text("SELECT lifecycle FROM user_memories WHERE id = :id"),
            {"id": memory_id},
        ).scalar_one()
        purge_count = connection.execute(
            text(
                "SELECT count(*) FROM outbox_events "
                "WHERE resource_id = :id AND event_type = 'user_memory.purge.requested'"
            ),
            {"id": memory_id},
        ).scalar_one()
    assert state == "expired"
    assert purge_count == 1


def test_unauthorized_forget_changes_nothing(
    lifecycle_store: tuple[TestClient, Engine, LifecycleService, MemoryScope],
) -> None:
    client, engine, lifecycle, scope = lifecycle_store
    memory_id, version_id = remember(engine, scope)

    response = client.delete(
        memory_path(scope, memory_id),
        params={"subject_id": str(scope.subject_id)},
        headers={"Authorization": "Bearer invalid", "Idempotency-Key": f"forget-{uuid4()}"},
    )

    assert response.status_code == 401
    assert lifecycle.is_retrievable(scope.tenant_id, memory_id, version_id) is True


def test_stale_version_is_rejected_after_current_pointer_moves(
    lifecycle_store: tuple[TestClient, Engine, LifecycleService, MemoryScope],
) -> None:
    _, engine, lifecycle, scope = lifecycle_store
    memory_id, old_version_id = remember(engine, scope)
    service = UserMemoryService(TenantDatabase(engine), "policy-2026-10")

    corrected = service.correct(
        CorrectionRequest(
            scope=scope,
            memory_id=memory_id,
            statement="The user prefers detailed answers.",
        ),
        f"correct-{uuid4()}",
    )

    assert lifecycle.is_retrievable(scope.tenant_id, memory_id, old_version_id) is False
    assert lifecycle.is_retrievable(
        scope.tenant_id, memory_id, corrected.receipt.resource_version
    ) is True

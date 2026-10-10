from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from memory_ops.api import create_app
from memory_ops.config import Settings
from memory_ops.persistence import (
    TenantDatabase,
    create_database_engine,
    upgrade_database,
)
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    SecurityBoundary,
    WorkspaceGrant,
)


@pytest.fixture(scope="module")
def api() -> tuple[TestClient, Engine, UUID, UUID, UUID]:
    settings = Settings.from_environment()
    engine = create_database_engine(settings.database_url)
    upgrade_database(engine)
    tenant_id, workspace_id, subject_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:tenant_id)"),
            {"tenant_id": tenant_id},
        )
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant_id)"),
            {"id": workspace_id, "tenant_id": tenant_id},
        )
    principal = AuthenticatedPrincipal(
        tenant_id=tenant_id,
        principal_id=uuid4(),
        workspace_grants=(
            WorkspaceGrant(
                workspace_id, frozenset({"memory:read", "memory:write"})
            ),
        ),
    )
    security = SecurityBoundary(
        credential_resolver=lambda credential: principal if credential == "valid" else None,
        policy=MachinePolicy(
            "policy-2026-10", frozenset({"memory:read", "memory:write"})
        ),
    )
    app = create_app(
        settings.model_copy(update={"environment": "test"}),
        security,
        TenantDatabase(engine),
    )
    with TestClient(app) as client:
        yield client, engine, tenant_id, workspace_id, subject_id
    engine.dispose()


def path(tenant_id: UUID, workspace_id: UUID, suffix: str = "memories") -> str:
    return f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}/{suffix}"


def payload(subject_id: UUID, statement: str = "The user prefers dark mode.") -> dict:
    return {
        "subject_id": str(subject_id),
        "semantic_type": "preference",
        "statement": statement,
        "sensitivity": "normal",
        "lifetime": "durable",
        "purpose": "assistant_context",
        "evidence": [
            {
                "evidence_type": "conversation_message",
                "reference_id": "message-42",
            }
        ],
    }


def headers(key: str) -> dict[str, str]:
    return {"Authorization": "Bearer valid", "Idempotency-Key": key}


def test_remember_inspect_list_and_operation_status_round_trip(
    api: tuple[TestClient, Engine, UUID, UUID, UUID],
) -> None:
    client, _, tenant_id, workspace_id, subject_id = api
    collection = path(tenant_id, workspace_id)
    created = client.post(collection, json=payload(subject_id), headers=headers("api-round-trip"))

    assert created.status_code == 201
    receipt = created.json()
    assert receipt["operation_status"] == "pending"
    assert receipt["replayed"] is False

    inspected = client.get(
        f"{collection}/{receipt['memory_id']}", headers=headers("unused")
    )
    assert inspected.status_code == 200
    memory = inspected.json()
    assert memory["statement"] == "The user prefers dark mode."
    assert memory["origin"] == "explicit"
    assert memory["evidence"][0]["reference_id"] == "message-42"

    listed = client.get(
        collection,
        params={"subject_id": str(subject_id), "purpose": "assistant_context"},
        headers=headers("unused"),
    )
    assert listed.status_code == 200
    assert [item["memory_id"] for item in listed.json()["items"]] == [
        receipt["memory_id"]
    ]

    operation = client.get(
        path(tenant_id, workspace_id, f"operations/{receipt['operation_id']}"),
        headers=headers("unused"),
    )
    assert operation.status_code == 200
    assert operation.json()["status"] == "pending"


def test_identical_and_conflicting_http_retries_have_stable_results(
    api: tuple[TestClient, Engine, UUID, UUID, UUID],
) -> None:
    client, _, tenant_id, workspace_id, subject_id = api
    collection = path(tenant_id, workspace_id)
    key = f"api-retry-{uuid4()}"
    first = client.post(collection, json=payload(subject_id), headers=headers(key))
    replay = client.post(collection, json=payload(subject_id), headers=headers(key))
    conflict = client.post(
        collection,
        json=payload(subject_id, "The user prefers light mode."),
        headers=headers(key),
    )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["memory_id"] == first.json()["memory_id"]
    assert conflict.status_code == 409
    assert conflict.json() == {
        "code": "conflict",
        "message": "idempotency key conflict",
    }


def test_correct_and_forget_round_trip(
    api: tuple[TestClient, Engine, UUID, UUID, UUID],
) -> None:
    client, _, tenant_id, workspace_id, subject_id = api
    collection = path(tenant_id, workspace_id)
    created = client.post(
        collection,
        json=payload(subject_id),
        headers=headers(f"api-lifecycle-create-{uuid4()}"),
    ).json()
    memory_path = f"{collection}/{created['memory_id']}"

    corrected = client.post(
        f"{memory_path}/corrections",
        json={
            "subject_id": str(subject_id),
            "statement": "The user prefers light mode.",
            "evidence": [],
        },
        headers=headers(f"api-lifecycle-correct-{uuid4()}"),
    )
    assert corrected.status_code == 201
    assert corrected.json()["memory_id"] == created["memory_id"]
    assert corrected.json()["version_id"] != created["version_id"]
    assert client.get(memory_path, headers=headers("unused")).json()["statement"] == (
        "The user prefers light mode."
    )

    forgotten = client.delete(
        memory_path,
        params={"subject_id": str(subject_id)},
        headers=headers(f"api-lifecycle-forget-{uuid4()}"),
    )
    assert forgotten.status_code == 202
    assert forgotten.json()["memory_id"] == created["memory_id"]
    assert client.get(memory_path, headers=headers("unused")).status_code == 404


def test_authorization_and_not_found_errors_do_not_disclose_content(
    api: tuple[TestClient, Engine, UUID, UUID, UUID],
) -> None:
    client, _, tenant_id, workspace_id, subject_id = api
    collection = path(tenant_id, workspace_id)

    unauthenticated = client.post(
        collection,
        json=payload(subject_id),
        headers={"Idempotency-Key": "missing-auth"},
    )
    denied = client.get(path(tenant_id, uuid4()), headers=headers("unused"))
    missing = client.get(f"{collection}/{uuid4()}", headers=headers("unused"))

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["code"] == "unauthenticated"
    assert denied.status_code == 403
    assert denied.json() == {"code": "permission_denied", "message": "permission denied"}
    assert missing.status_code == 404
    assert missing.json() == {"code": "not_found", "message": "resource not found"}


def test_validation_and_secret_admission_errors_are_stable_and_content_free(
    api: tuple[TestClient, Engine, UUID, UUID, UUID],
) -> None:
    client, _, tenant_id, workspace_id, subject_id = api
    collection = path(tenant_id, workspace_id)
    invalid = client.post(collection, json={}, headers=headers("invalid"))
    secret = "api_key=sk-test-only-prohibited-sentinel"
    prohibited = client.post(
        collection,
        json=payload(subject_id, secret),
        headers=headers("secret"),
    )

    assert invalid.status_code == 400
    assert invalid.json() == {"code": "invalid_request", "message": "invalid request"}
    assert prohibited.status_code == 400
    assert prohibited.json() == {
        "code": "invalid_request",
        "message": "content is prohibited by policy",
    }
    assert secret not in prohibited.text


def test_versioned_openapi_publishes_memory_and_operation_routes(
    api: tuple[TestClient, Engine, UUID, UUID, UUID],
) -> None:
    client, _, _, _, _ = api
    paths = client.get("/v1/openapi.json").json()["paths"]

    assert "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/memories" in paths
    assert (
        "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/memories/{memory_id}"
        in paths
    )
    assert (
        "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/operations/{operation_id}"
        in paths
    )

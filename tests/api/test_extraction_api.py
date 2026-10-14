from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from memory_ops.api import create_app
from memory_ops.config import Settings
from memory_ops.extraction import ExtractionPolicy, ModelIdentity, ShadowExtractionPipeline
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    SecurityBoundary,
    WorkspaceGrant,
)


MODEL = ModelIdentity(
    provider="test-provider", model_name="test-extractor", version="test-v1"
)


@pytest.fixture(scope="module")
def api() -> tuple[TestClient, Engine, UUID, UUID, UUID, list[str]]:
    settings = Settings.from_environment().model_copy(
        update={
            "environment": "test",
            "azure_openai_base_url": None,
            "azure_openai_api_key": None,
            "azure_openai_deployment": None,
        }
    )
    engine = create_database_engine(settings.database_url)
    upgrade_database(engine)
    tenant_id, workspace_id, subject_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:id)"), {"id": tenant_id}
        )
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant_id)"),
            {"id": workspace_id, "tenant_id": tenant_id},
        )
    principal = AuthenticatedPrincipal(
        tenant_id=tenant_id,
        principal_id=uuid4(),
        workspace_grants=(
            WorkspaceGrant(workspace_id, frozenset({"memory:write"})),
        ),
    )
    security = SecurityBoundary(
        credential_resolver=lambda credential: principal if credential == "valid" else None,
        policy=MachinePolicy("policy-test", frozenset({"memory:write"})),
    )
    calls: list[str] = []

    def extractor(content: str):
        calls.append(content)
        return [
            {
                "statement": "The user prefers aisle seats.",
                "semantic_type": "preference",
                "confidence": 0.98,
                "explanation": "An explicit recurring preference.",
            }
        ]

    pipeline = ShadowExtractionPipeline(
        ExtractionPolicy(
            version="policy-test",
            allowed_categories=frozenset(
                {"fact", "preference", "goal", "constraint", "episode"}
            ),
            allowed_external_models=(MODEL,),
            allowed_external_sensitivities=frozenset({"normal"}),
        ),
        MODEL,
        extractor,
    )
    app = create_app(settings, security, TenantDatabase(engine), extraction_pipeline=pipeline)
    with TestClient(app) as client:
        yield client, engine, tenant_id, workspace_id, subject_id, calls
    engine.dispose()


def endpoint(tenant_id: UUID, workspace_id: UUID) -> str:
    return f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}/memory-extractions"


def body(subject_id: UUID, content: str = "I prefer aisle seats.") -> dict:
    return {
        "subject_id": str(subject_id),
        "reference_id": "message-42",
        "content": content,
        "sensitivity": "normal",
    }


def memory_count(engine: Engine, tenant_id: UUID) -> int:
    with engine.begin() as connection:
        return connection.execute(
            text("SELECT count(*) FROM user_memories WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one()


def test_message_extraction_returns_shadow_candidate_and_writes_nothing(api) -> None:
    client, engine, tenant_id, workspace_id, subject_id, calls = api
    before = memory_count(engine, tenant_id)

    response = client.post(
        endpoint(tenant_id, workspace_id),
        json=body(subject_id),
        headers={"Authorization": "Bearer valid"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "candidates"
    assert result["canonical_write_count"] == 0
    assert result["candidates"][0]["canonical_persisted"] is False
    assert result["candidates"][0]["semantic_type"] == "preference"
    assert result["candidates"][0]["source_reference_id"] == "message-42"
    assert calls[-1] == "I prefer aisle seats."
    assert memory_count(engine, tenant_id) == before


def test_policy_denial_and_authentication_fail_before_model(api) -> None:
    client, _, tenant_id, workspace_id, subject_id, calls = api
    call_count = len(calls)
    secret = "api_key=sk-test-only-prohibited-sentinel"

    denied = client.post(
        endpoint(tenant_id, workspace_id),
        json=body(subject_id, secret),
        headers={"Authorization": "Bearer valid"},
    )
    unauthenticated = client.post(
        endpoint(tenant_id, workspace_id), json=body(subject_id)
    )

    assert denied.status_code == 200
    assert denied.json()["status"] == "denied"
    assert denied.json()["reason_codes"] == ["prohibited_content"]
    assert secret not in denied.text
    assert unauthenticated.status_code == 401
    assert len(calls) == call_count


def test_unconfigured_extractor_degrades_explicitly(api) -> None:
    _, engine, tenant_id, workspace_id, subject_id, _ = api
    settings = Settings.from_environment().model_copy(
        update={
            "environment": "test",
            "azure_openai_base_url": None,
            "azure_openai_api_key": None,
            "azure_openai_deployment": None,
        }
    )
    principal = AuthenticatedPrincipal(
        tenant_id=tenant_id,
        principal_id=uuid4(),
        workspace_grants=(WorkspaceGrant(workspace_id, frozenset({"memory:write"})),),
    )
    security = SecurityBoundary(
        credential_resolver=lambda credential: principal if credential == "valid" else None,
        policy=MachinePolicy("policy-test", frozenset({"memory:write"})),
    )
    app = create_app(settings, security, TenantDatabase(engine))

    with TestClient(app) as client:
        response = client.post(
            endpoint(tenant_id, workspace_id),
            json=body(subject_id),
            headers={"Authorization": "Bearer valid"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["reason_codes"] == ["extractor_unconfigured"]
    assert response.json()["canonical_write_count"] == 0

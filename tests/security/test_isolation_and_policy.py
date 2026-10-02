from uuid import UUID

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from memory_ops.api import create_app
from memory_ops.api.security import authorize_request
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    PermissionDenied,
    ProhibitedContent,
    ResourceScope,
    SecurityBoundary,
    Unauthenticated,
    WorkspaceGrant,
    enforce_content_admission,
)


TENANT = UUID("00000000-0000-0000-0000-000000000001")
OTHER_TENANT = UUID("00000000-0000-0000-0000-000000000002")
WORKSPACE = UUID("00000000-0000-0000-0000-000000000010")
OTHER_WORKSPACE = UUID("00000000-0000-0000-0000-000000000020")
PRINCIPAL = UUID("00000000-0000-0000-0000-000000000100")


def boundary() -> SecurityBoundary:
    principal = AuthenticatedPrincipal(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        workspace_grants=(WorkspaceGrant(WORKSPACE, frozenset({"memory:read"})),),
    )
    return SecurityBoundary(
        credential_resolver=lambda credential: principal if credential == "valid" else None,
        policy=MachinePolicy("2026-10-02", frozenset({"memory:read", "memory:write"})),
    )


def test_authorization_is_authenticated_explicit_and_deny_by_default() -> None:
    security = boundary()
    resource = ResourceScope(TENANT, WORKSPACE)

    assert security.authorize("valid", resource, "memory:read").principal_id == PRINCIPAL

    with pytest.raises(Unauthenticated):
        security.authorize(None, resource, "memory:read")
    with pytest.raises(Unauthenticated):
        security.authorize("invalid", resource, "memory:read")
    with pytest.raises(PermissionDenied):
        security.authorize("valid", resource, "memory:write")
    with pytest.raises(PermissionDenied):
        security.authorize("valid", resource, "memory:unknown")


def test_tenant_and_workspace_denials_do_not_disclose_which_scope_failed() -> None:
    security = boundary()

    with pytest.raises(PermissionDenied) as tenant_denial:
        security.authorize(
            "valid", ResourceScope(OTHER_TENANT, WORKSPACE), "memory:read"
        )
    with pytest.raises(PermissionDenied) as workspace_denial:
        security.authorize(
            "valid", ResourceScope(TENANT, OTHER_WORKSPACE), "memory:read"
        )

    assert tenant_denial.value.code == workspace_denial.value.code
    assert str(tenant_denial.value) == str(workspace_denial.value) == "permission denied"


def test_authentication_and_policy_outages_fail_closed() -> None:
    resource = ResourceScope(TENANT, WORKSPACE)

    with pytest.raises(Unauthenticated):
        SecurityBoundary().authorize("credential", resource, "memory:read")
    with pytest.raises(PermissionDenied):
        SecurityBoundary(credential_resolver=lambda _: boundary().credential_resolver("valid")).authorize(
            "credential", resource, "memory:read"
        )


@pytest.mark.parametrize(
    "content",
    [
        "password=hunter2",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "-----BEGIN PRIVATE KEY-----\nsecret",
        "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
        "CVV: 123",
        "AWS key AKIAIOSFODNN7EXAMPLE",
        "GitHub ghp_abcdefghijklmnopqrstuvwxyz123456",
        "OpenAI sk-abcdefghijklmnopqrstuvwxyz123456",
    ],
)
def test_prohibited_secrets_are_rejected_without_echo(content: str) -> None:
    with pytest.raises(ProhibitedContent) as denial:
        enforce_content_admission(content)

    assert content not in str(denial.value)


def test_ordinary_memory_content_is_admitted() -> None:
    enforce_content_admission("The user prefers password managers and short meetings.")


def test_http_adapter_accepts_only_bearer_credentials_from_trusted_app_state() -> None:
    app = create_app(security=boundary())

    @app.get("/v1/test")
    def protected(request: Request) -> dict[str, str]:
        principal = authorize_request(
            request, ResourceScope(TENANT, WORKSPACE), "memory:read"
        )
        return {"principal_id": str(principal.principal_id)}

    client = TestClient(app, raise_server_exceptions=False)
    denied = client.get("/v1/test", headers={"Authorization": "Basic valid"})
    assert denied.status_code == 401
    assert denied.json() == {
        "code": "unauthenticated",
        "message": "authentication required",
    }
    assert client.get("/v1/test", headers={"Authorization": "Bearer valid"}).json() == {
        "principal_id": str(PRINCIPAL)
    }

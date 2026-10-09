"""HTTP adapter for the service security boundary."""

import secrets

from fastapi import Request

from memory_ops.config import Settings
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    ResourceScope,
    SecurityBoundary,
    WorkspaceGrant,
)


_API_ACTIONS = frozenset(
    {
        "lesson:monitor",
        "lesson:promote",
        "lesson:rollback",
        "memory:read",
        "memory:write",
    }
)


def configured_security_boundary(settings: Settings) -> SecurityBoundary:
    """Build the fail-closed boundary for one local test API credential."""

    if settings.api_token is None:
        return SecurityBoundary()

    expected = settings.api_token.get_secret_value()
    tenant_id = settings.api_tenant_id
    workspace_id = settings.api_workspace_id
    principal_id = settings.api_principal_id
    if tenant_id is None or workspace_id is None or principal_id is None:
        return SecurityBoundary()

    principal = AuthenticatedPrincipal(
        tenant_id=tenant_id,
        principal_id=principal_id,
        workspace_grants=(WorkspaceGrant(workspace_id, _API_ACTIONS),),
    )

    def resolve(credential: str) -> AuthenticatedPrincipal | None:
        return principal if secrets.compare_digest(credential, expected) else None

    return SecurityBoundary(
        credential_resolver=resolve,
        policy=MachinePolicy("local-test-token-v1", _API_ACTIONS),
    )


def authorize_request(
    request: Request,
    resource: ResourceScope,
    action: str,
) -> AuthenticatedPrincipal:
    """Authorize using trusted app state and canonical resource ownership."""

    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    token = credential if scheme.lower() == "bearer" and credential else None
    boundary: SecurityBoundary = request.app.state.security
    return boundary.authorize(token, resource, action)

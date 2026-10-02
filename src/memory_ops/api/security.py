"""HTTP adapter for the service security boundary."""

from fastapi import Request

from memory_ops.security import AuthenticatedPrincipal, ResourceScope, SecurityBoundary


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

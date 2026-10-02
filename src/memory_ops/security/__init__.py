"""Fail-closed authentication, authorization, and content admission."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID


class SecurityError(Exception):
    """A safe-to-return security failure."""

    code = "permission_denied"
    message = "permission denied"

    def __init__(self) -> None:
        super().__init__(self.message)


class Unauthenticated(SecurityError):
    code = "unauthenticated"
    message = "authentication required"


class PermissionDenied(SecurityError):
    pass


class ProhibitedContent(SecurityError):
    code = "invalid_request"
    message = "content is prohibited by policy"


@dataclass(frozen=True)
class WorkspaceGrant:
    workspace_id: UUID
    actions: frozenset[str]


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Identity and grants produced by a trusted authentication adapter."""

    tenant_id: UUID
    principal_id: UUID
    workspace_grants: tuple[WorkspaceGrant, ...] = ()


@dataclass(frozen=True)
class ResourceScope:
    """Canonical resource ownership resolved by the service."""

    tenant_id: UUID
    workspace_id: UUID


@dataclass(frozen=True)
class MachinePolicy:
    """Versioned, explicit allow policy. Every unmatched request is denied."""

    version: str
    allowed_actions: frozenset[str]

    def authorize(
        self,
        principal: AuthenticatedPrincipal,
        resource: ResourceScope,
        action: str,
    ) -> None:
        if not self.version or action not in self.allowed_actions:
            raise PermissionDenied
        if principal.tenant_id != resource.tenant_id:
            raise PermissionDenied
        if not any(
            grant.workspace_id == resource.workspace_id and action in grant.actions
            for grant in principal.workspace_grants
        ):
            raise PermissionDenied


CredentialResolver = Callable[[str], AuthenticatedPrincipal | None]


@dataclass(frozen=True)
class SecurityBoundary:
    """Authenticate first, then enforce current machine policy."""

    credential_resolver: CredentialResolver | None = None
    policy: MachinePolicy | None = None

    def authorize(
        self,
        credential: str | None,
        resource: ResourceScope,
        action: str,
    ) -> AuthenticatedPrincipal:
        if not credential:
            raise Unauthenticated
        if self.credential_resolver is None:
            raise Unauthenticated

        try:
            principal = self.credential_resolver(credential)
        except Exception as error:
            raise Unauthenticated from error
        if principal is None:
            raise Unauthenticated
        if self.policy is None:
            raise PermissionDenied

        try:
            self.policy.authorize(principal, resource, action)
        except SecurityError:
            raise
        except Exception as error:
            raise PermissionDenied from error
        return principal


_PROHIBITED_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}",
        r"\b(?:password|passwd|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|authorization)\b\s*[:=]\s*\S+",
        r"\b(?:cvv|cvc|card security code)\b\s*[:=]\s*\d{3,4}\b",
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bsk-[A-Za-z0-9_-]{20,}\b",
    )
)


def enforce_content_admission(content: str) -> None:
    """Reject compromise-enabling secrets without echoing matched content."""

    if any(pattern.search(content) for pattern in _PROHIBITED_SECRET_PATTERNS):
        raise ProhibitedContent

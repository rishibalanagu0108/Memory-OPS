from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from memory_ops.operations import RuntimeGuard
from memory_ops.persistence import TenantDatabase
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    PermissionDenied,
    ResourceScope,
    SecurityBoundary,
    Unauthenticated,
    WorkspaceGrant,
)


def test_identity_and_policy_faults_fail_closed_without_database_access() -> None:
    tenant_id, workspace_id = uuid4(), uuid4()
    resource = ResourceScope(tenant_id, workspace_id)

    with pytest.raises(Unauthenticated):
        SecurityBoundary(credential_resolver=lambda _: (_ for _ in ()).throw(RuntimeError("offline"))).authorize("valid", resource, "memory:read")

    principal = AuthenticatedPrincipal(
        tenant_id,
        uuid4(),
        (WorkspaceGrant(workspace_id, frozenset({"memory:read"})),),
    )
    with pytest.raises(PermissionDenied):
        SecurityBoundary(credential_resolver=lambda _: principal).authorize("valid", resource, "memory:read")


def test_overload_and_drain_faults_are_explicit() -> None:
    engine = create_engine("sqlite://")
    boundary = SecurityBoundary(
        credential_resolver=lambda _: None,
        policy=MachinePolicy("m8", frozenset()),
    )
    guard = RuntimeGuard(TenantDatabase(engine), boundary, max_in_flight=1)
    try:
        assert guard.admit() == "admitted"
        assert guard.admit() == "quota"
        guard.release()
        guard.start_draining()
        assert guard.admit() == "draining"
    finally:
        engine.dispose()

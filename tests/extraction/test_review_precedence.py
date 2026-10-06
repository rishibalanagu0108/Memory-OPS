from uuid import UUID, uuid4

import pytest

from memory_ops.extraction import (
    CandidateReviewService,
    ExtractionCandidate,
    ModelIdentity,
)
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    PermissionDenied,
    SecurityBoundary,
    WorkspaceGrant,
)
from memory_ops.user_memory import MemoryScope


TENANT = UUID("00000000-0000-0000-0000-000000000001")
WORKSPACE = UUID("00000000-0000-0000-0000-000000000010")
REVIEWER = UUID("00000000-0000-0000-0000-000000000100")


def candidate() -> ExtractionCandidate:
    return ExtractionCandidate(
        scope=MemoryScope(
            tenant_id=TENANT,
            workspace_id=WORKSPACE,
            subject_id=uuid4(),
        ),
        statement="The user prefers window seats.",
        semantic_type="preference",
        confidence=0.97,
        explanation="The user states a recurring preference.",
        source_reference_id="message-42",
        policy_version="policy-1",
        model=ModelIdentity(
            provider="example-provider",
            model_name="extractor-small",
            version="2026-10-06",
        ),
    )


def boundary(actions: frozenset[str] = frozenset({"memory:review"})) -> SecurityBoundary:
    principal = AuthenticatedPrincipal(
        tenant_id=TENANT,
        principal_id=REVIEWER,
        workspace_grants=(WorkspaceGrant(WORKSPACE, actions),),
    )
    return SecurityBoundary(
        credential_resolver=lambda credential: principal if credential == "valid" else None,
        policy=MachinePolicy("policy-1", actions),
    )


def service(
    *, policy_allowed: bool = True, explicit_operation: str | None = None
) -> CandidateReviewService:
    return CandidateReviewService(
        boundary(),
        policy_allows=lambda _: policy_allowed,
        explicit_operation=lambda _: explicit_operation,
    )


def test_authorized_review_records_a_non_persisting_decision() -> None:
    reviewed = service().review("valid", candidate(), "approve")

    assert reviewed.reviewer_id == REVIEWER
    assert reviewed.outcome == "approved"
    assert reviewed.reason_code == "reviewer_approved"
    assert reviewed.canonical_persisted is False

    rejected = service().review("valid", candidate(), "reject")
    assert rejected.outcome == "rejected"
    assert rejected.reason_code == "reviewer_rejected"


def test_review_requires_explicit_workspace_authorization() -> None:
    with pytest.raises(PermissionDenied):
        CandidateReviewService(
            boundary(frozenset()),
            policy_allows=lambda _: True,
            explicit_operation=lambda _: None,
        ).review("valid", candidate(), "approve")


def test_policy_denial_outranks_reviewer_approval() -> None:
    reviewed = service(policy_allowed=False).review("valid", candidate(), "approve")

    assert reviewed.outcome == "blocked"
    assert reviewed.reason_code == "policy_denial"
    assert reviewed.canonical_persisted is False


@pytest.mark.parametrize("operation", ["remember", "correct", "forget"])
def test_explicit_operations_outrank_reviewer_approval(operation: str) -> None:
    reviewed = service(explicit_operation=operation).review(
        "valid", candidate(), "approve"
    )

    assert reviewed.outcome == "blocked"
    assert reviewed.reason_code == f"explicit_{operation}_precedence"
    assert reviewed.canonical_persisted is False


def test_precedence_lookup_failure_blocks_review() -> None:
    def unavailable(_: ExtractionCandidate) -> None:
        raise RuntimeError("state unavailable")

    reviewed = CandidateReviewService(
        boundary(),
        policy_allows=lambda _: True,
        explicit_operation=unavailable,
    ).review("valid", candidate(), "approve")

    assert reviewed.outcome == "blocked"
    assert reviewed.reason_code == "explicit_operation_unavailable"

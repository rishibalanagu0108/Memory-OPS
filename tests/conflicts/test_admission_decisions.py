from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from memory_ops.user_memory import (
    AdmissionCandidate,
    AdmissionKind,
    CanonicalMemoryVersion,
    GovernanceMetadata,
    MemoryScope,
    decide_admission,
)


NOW = datetime(2026, 10, 5, tzinfo=UTC)


def existing_version(**changes: object) -> CanonicalMemoryVersion:
    values = {
        "id": uuid4(),
        "memory_id": uuid4(),
        "version_number": 1,
        "scope": MemoryScope(
            tenant_id=uuid4(),
            workspace_id=uuid4(),
            subject_id=uuid4(),
        ),
        "semantic_type": "preference",
        "original_statement": "The user prefers tea.",
        "normalized_subject": "user",
        "normalized_predicate": "preferred_drink",
        "normalized_value": "tea",
        "valid_from": NOW,
        "recorded_at": NOW,
        "governance": GovernanceMetadata(
            sensitivity="normal",
            lifetime="durable",
            origin="explicit",
            purpose="assistant_context",
            policy_version="policy-2026-10",
        ),
    }
    values.update(changes)
    return CanonicalMemoryVersion.model_validate(values)


def candidate(existing: CanonicalMemoryVersion, **changes: object) -> AdmissionCandidate:
    values = {
        "normalized_subject": existing.normalized_subject,
        "normalized_predicate": existing.normalized_predicate,
        "normalized_value": existing.normalized_value,
        "qualifiers": existing.qualifiers,
        "valid_from": existing.valid_from,
    }
    values.update(changes)
    return AdmissionCandidate.model_validate(values)


def test_equivalent_candidate_is_a_duplicate() -> None:
    existing = existing_version()

    decision = decide_admission(existing, candidate(existing))

    assert decision.kind is AdmissionKind.DUPLICATE
    assert decision.automatic is True
    assert decision.requires_confirmation is False


def test_distinct_qualifiers_coexist() -> None:
    existing = existing_version(qualifiers={"context": "home"})

    decision = decide_admission(
        existing,
        candidate(existing, normalized_value="coffee", qualifiers={"context": "work"}),
    )

    assert decision.kind is AdmissionKind.QUALIFIED_COEXISTENCE
    assert decision.automatic is True


def test_later_effective_value_is_a_temporal_change() -> None:
    existing = existing_version()

    decision = decide_admission(
        existing,
        candidate(
            existing,
            normalized_value="coffee",
            valid_from=existing.valid_from + timedelta(days=1),
        ),
    )

    assert decision.kind is AdmissionKind.TEMPORAL_CHANGE
    assert decision.automatic is True


def test_explicit_targeted_correction_overrides_other_classification() -> None:
    existing = existing_version()

    decision = decide_admission(
        existing,
        candidate(
            existing,
            normalized_value="coffee",
            correction_of_version_id=existing.id,
        ),
    )

    assert decision.kind is AdmissionKind.CORRECTION
    assert decision.automatic is True


def test_same_time_contradiction_requires_confirmation() -> None:
    existing = existing_version()

    decision = decide_admission(
        existing,
        candidate(existing, normalized_value="coffee"),
    )

    assert decision.kind is AdmissionKind.AMBIGUOUS_CONFLICT
    assert decision.automatic is False
    assert decision.requires_confirmation is True


def test_unmatched_or_mistargeted_candidates_are_not_silently_classified() -> None:
    existing = existing_version()

    with pytest.raises(ValueError, match="same semantic identity"):
        decide_admission(
            existing,
            candidate(existing, normalized_predicate="preferred_color"),
        )
    with pytest.raises(ValueError, match="identify the matched version"):
        decide_admission(
            existing,
            candidate(existing, correction_of_version_id=uuid4()),
        )

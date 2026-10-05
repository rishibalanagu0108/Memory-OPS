"""Deterministic admission decisions for comparable memory candidates."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field

from memory_ops.user_memory import BoundedText, CanonicalMemoryVersion, DomainModel


class AdmissionKind(StrEnum):
    DUPLICATE = "duplicate"
    QUALIFIED_COEXISTENCE = "qualified_coexistence"
    TEMPORAL_CHANGE = "temporal_change"
    CORRECTION = "correction"
    AMBIGUOUS_CONFLICT = "ambiguous_conflict"


class AdmissionCandidate(DomainModel):
    normalized_subject: BoundedText
    normalized_predicate: BoundedText
    normalized_value: object
    qualifiers: dict[str, object] = Field(default_factory=dict)
    valid_from: datetime
    correction_of_version_id: UUID | None = None


class AdmissionDecision(DomainModel):
    kind: AdmissionKind
    automatic: bool
    requires_confirmation: bool = False
    matched_version_id: UUID


def decide_admission(
    existing: CanonicalMemoryVersion, candidate: AdmissionCandidate
) -> AdmissionDecision:
    """Classify a structured candidate without probabilistic interpretation."""

    if (
        candidate.normalized_subject != existing.normalized_subject
        or candidate.normalized_predicate != existing.normalized_predicate
    ):
        raise ValueError("admission candidates must have the same semantic identity")

    if candidate.correction_of_version_id is not None:
        if candidate.correction_of_version_id != existing.id:
            raise ValueError("explicit correction must identify the matched version")
        kind = AdmissionKind.CORRECTION
    elif candidate.qualifiers != existing.qualifiers:
        kind = AdmissionKind.QUALIFIED_COEXISTENCE
    elif candidate.normalized_value == existing.normalized_value:
        kind = AdmissionKind.DUPLICATE
    elif candidate.valid_from > existing.valid_from:
        kind = AdmissionKind.TEMPORAL_CHANGE
    else:
        kind = AdmissionKind.AMBIGUOUS_CONFLICT

    ambiguous = kind is AdmissionKind.AMBIGUOUS_CONFLICT
    return AdmissionDecision(
        kind=kind,
        automatic=not ambiguous,
        requires_confirmation=ambiguous,
        matched_version_id=existing.id,
    )

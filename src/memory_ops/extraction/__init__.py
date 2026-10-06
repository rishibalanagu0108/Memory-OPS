"""Policy-controlled user-memory extraction that cannot perform writes."""

from collections.abc import Callable, Iterable, Mapping
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator

from memory_ops.security import ProhibitedContent, enforce_content_admission
from memory_ops.user_memory import (
    BoundedText,
    DomainModel,
    MemoryScope,
    SemanticType,
    Sensitivity,
)


class ModelIdentity(DomainModel):
    provider: BoundedText
    model_name: BoundedText
    version: BoundedText
    external: bool = True


class ExtractionSource(DomainModel):
    reference_id: BoundedText
    content: Annotated[str, Field(min_length=1, max_length=10_000)]
    sensitivity: Sensitivity = "normal"

    @field_validator("content")
    @classmethod
    def content_has_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must contain non-whitespace text")
        return value


class ExtractionDraft(DomainModel):
    statement: Annotated[str, Field(min_length=1, max_length=10_000)]
    semantic_type: SemanticType
    confidence: Annotated[float, Field(ge=0, le=1)]
    explanation: Annotated[str, Field(min_length=1, max_length=1_000)]


class ExtractionPolicy(DomainModel):
    version: BoundedText
    allowed_categories: frozenset[SemanticType]
    allowed_external_models: tuple[ModelIdentity, ...] = ()
    allowed_external_sensitivities: frozenset[Sensitivity] = frozenset()

    def permits(self, model: ModelIdentity, sensitivity: Sensitivity) -> bool:
        if not model.external:
            return True
        return (
            model in self.allowed_external_models
            and sensitivity in self.allowed_external_sensitivities
        )


class ExtractionCandidate(DomainModel):
    scope: MemoryScope
    statement: Annotated[str, Field(min_length=1, max_length=10_000)]
    semantic_type: SemanticType
    confidence: Annotated[float, Field(ge=0, le=1)]
    explanation: Annotated[str, Field(min_length=1, max_length=1_000)]
    source_reference_id: BoundedText
    policy_version: BoundedText
    model: ModelIdentity
    mode: Literal["shadow"] = "shadow"
    canonical_persisted: Literal[False] = False


class ModelTrace(DomainModel):
    """Content-free metadata suitable for operational telemetry."""

    provider: BoundedText
    model_name: BoundedText
    model_version: BoundedText
    policy_version: BoundedText
    outcome: Literal["completed", "failed"]


class ExtractionResult(DomainModel):
    status: Literal["candidates", "abstained", "denied", "failed"]
    candidates: tuple[ExtractionCandidate, ...] = ()
    reason_codes: tuple[BoundedText, ...] = ()
    model_trace: ModelTrace | None = None
    mode: Literal["shadow"] = "shadow"
    canonical_write_count: Literal[0] = 0


Extractor = Callable[
    [str], Iterable[ExtractionDraft | Mapping[str, object]]
]


class ShadowExtractionPipeline:
    """Produce reviewable candidates without access to canonical persistence."""

    def __init__(
        self,
        policy: ExtractionPolicy,
        model: ModelIdentity,
        extractor: Extractor,
    ) -> None:
        self.policy = policy
        self.model = model
        self.extractor = extractor

    def extract(
        self, source: ExtractionSource, scope: MemoryScope
    ) -> ExtractionResult:
        if not self.policy.permits(self.model, source.sensitivity):
            return ExtractionResult(
                status="denied", reason_codes=("external_model_denied",)
            )
        try:
            enforce_content_admission(source.content)
        except ProhibitedContent:
            return ExtractionResult(
                status="denied", reason_codes=("prohibited_content",)
            )

        trace_values = {
            "provider": self.model.provider,
            "model_name": self.model.model_name,
            "model_version": self.model.version,
            "policy_version": self.policy.version,
        }
        try:
            drafts = tuple(
                draft
                if isinstance(draft, ExtractionDraft)
                else ExtractionDraft.model_validate(draft)
                for draft in self.extractor(source.content)
            )
            for draft in drafts:
                enforce_content_admission(draft.statement)
        except ProhibitedContent:
            return ExtractionResult(
                status="denied",
                reason_codes=("prohibited_content",),
                model_trace=ModelTrace(**trace_values, outcome="completed"),
            )
        except (TypeError, ValueError, ValidationError):
            return ExtractionResult(
                status="failed",
                reason_codes=("invalid_model_output",),
                model_trace=ModelTrace(**trace_values, outcome="failed"),
            )
        except Exception:
            return ExtractionResult(
                status="failed",
                reason_codes=("model_unavailable",),
                model_trace=ModelTrace(**trace_values, outcome="failed"),
            )

        candidates = tuple(
            ExtractionCandidate(
                scope=scope,
                statement=draft.statement,
                semantic_type=draft.semantic_type,
                confidence=draft.confidence,
                explanation=draft.explanation,
                source_reference_id=source.reference_id,
                policy_version=self.policy.version,
                model=self.model,
            )
            for draft in drafts
            if draft.semantic_type in self.policy.allowed_categories
        )
        denied_category = len(candidates) != len(drafts)
        return ExtractionResult(
            status="candidates" if candidates else "abstained",
            candidates=candidates,
            reason_codes=("category_denied",) if denied_category else (),
            model_trace=ModelTrace(**trace_values, outcome="completed"),
        )


__all__ = [
    "ExtractionCandidate",
    "ExtractionDraft",
    "ExtractionPolicy",
    "ExtractionResult",
    "ExtractionSource",
    "ModelIdentity",
    "ModelTrace",
    "ShadowExtractionPipeline",
]

from uuid import uuid4

from memory_ops.extraction import (
    ExtractionPolicy,
    ExtractionSource,
    ModelIdentity,
    ShadowExtractionPipeline,
)
from memory_ops.user_memory import MemoryScope


MODEL = ModelIdentity(
    provider="example-provider",
    model_name="extractor-small",
    version="2026-10-06",
)
SCOPE = MemoryScope(
    tenant_id=uuid4(),
    workspace_id=uuid4(),
    subject_id=uuid4(),
)


def policy(**changes: object) -> ExtractionPolicy:
    values = {
        "version": "policy-1",
        "allowed_categories": frozenset(
            {"fact", "preference", "goal", "constraint", "episode"}
        ),
        "allowed_external_models": (MODEL,),
        "allowed_external_sensitivities": frozenset({"normal"}),
    }
    values.update(changes)
    return ExtractionPolicy.model_validate(values)


def test_shadow_mode_emits_explainable_candidates_without_writes() -> None:
    source = ExtractionSource(
        reference_id="message-42",
        content="I prefer window seats.",
    )
    pipeline = ShadowExtractionPipeline(
        policy(),
        MODEL,
        lambda content: [
            {
                "statement": content,
                "semantic_type": "preference",
                "confidence": 0.97,
                "explanation": "The user states a recurring personal preference.",
            }
        ],
    )

    result = pipeline.extract(source, SCOPE)

    assert result.status == "candidates"
    assert result.canonical_write_count == 0
    assert result.candidates[0].canonical_persisted is False
    assert result.candidates[0].scope == SCOPE
    assert result.candidates[0].explanation
    assert result.candidates[0].source_reference_id == "message-42"
    assert result.model_trace is not None
    assert result.model_trace.model_dump() == {
        "provider": "example-provider",
        "model_name": "extractor-small",
        "model_version": "2026-10-06",
        "policy_version": "policy-1",
        "outcome": "completed",
    }
    assert source.content not in result.model_trace.model_dump_json()


def test_policy_denial_prevents_external_model_invocation() -> None:
    invoked = False

    def extractor(_: str) -> list[object]:
        nonlocal invoked
        invoked = True
        return []

    pipeline = ShadowExtractionPipeline(
        policy(allowed_external_sensitivities=frozenset()), MODEL, extractor
    )

    result = pipeline.extract(
        ExtractionSource(
            reference_id="message-sensitive",
            content="My health information is private.",
            sensitivity="sensitive",
        ),
        SCOPE,
    )

    assert result.status == "denied"
    assert result.reason_codes == ("external_model_denied",)
    assert invoked is False


def test_prohibited_content_never_reaches_the_model_or_candidates() -> None:
    invoked = False

    def extractor(_: str) -> list[object]:
        nonlocal invoked
        invoked = True
        return []

    result = ShadowExtractionPipeline(policy(), MODEL, extractor).extract(
        ExtractionSource(
            reference_id="message-secret",
            content="password=hunter2",
        ),
        SCOPE,
    )

    assert result.status == "denied"
    assert result.reason_codes == ("prohibited_content",)
    assert result.candidates == ()
    assert result.canonical_write_count == 0
    assert invoked is False


def test_unapproved_categories_and_invalid_model_output_fail_closed() -> None:
    restricted = policy(allowed_categories=frozenset({"preference"}))
    fact = {
        "statement": "The Atlas deadline is Friday.",
        "semantic_type": "fact",
        "confidence": 0.95,
        "explanation": "The user states a project date.",
    }
    denied = ShadowExtractionPipeline(restricted, MODEL, lambda _: [fact]).extract(
        ExtractionSource(reference_id="message-fact", content="Deadline is Friday."),
        SCOPE,
    )
    invalid = ShadowExtractionPipeline(policy(), MODEL, lambda _: [{"bad": True}]).extract(
        ExtractionSource(reference_id="message-invalid", content="Remember this."),
        SCOPE,
    )

    assert denied.status == "abstained"
    assert denied.reason_codes == ("category_denied",)
    assert denied.candidates == ()
    assert invalid.status == "failed"
    assert invalid.reason_codes == ("invalid_model_output",)

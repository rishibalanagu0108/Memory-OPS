from uuid import UUID, uuid4

import pytest

from memory_ops.retrieval import (
    EmbeddingModel,
    HybridNotPromoted,
    PromotionDecision,
    RetrievalCandidate,
    RetrievalMeasurements,
    RetrievalService,
    evaluate_rrf_promotion,
    reciprocal_rank_fusion,
)
from memory_ops.user_memory import MemoryScope


def candidate(memory_id: str, score: float, channel: str) -> RetrievalCandidate:
    return RetrievalCandidate(
        memory_id=UUID(memory_id),
        version_id=uuid4(),
        statement=memory_id,
        semantic_type="fact",
        purpose="planning",
        score=score,
        channel=channel,
    )


def test_rrf_combines_ranks_without_comparing_raw_scores() -> None:
    first = candidate("10000000-0000-0000-0000-000000000001", 0.9, "keyword")
    second = candidate("10000000-0000-0000-0000-000000000002", 0.8, "keyword")
    vector_first = candidate(str(second.memory_id), 0.01, "vector")

    fused = reciprocal_rank_fusion(((first, second), (vector_first,)), rank_constant=60)

    assert [item.memory_id for item in fused] == [second.memory_id, first.memory_id]
    assert fused[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert all(item.channel == "hybrid" for item in fused)


def test_rrf_keeps_equal_score_ties_at_the_same_rank() -> None:
    first = candidate("10000000-0000-0000-0000-000000000001", 0.5, "keyword")
    second = candidate("10000000-0000-0000-0000-000000000002", 0.5, "keyword")

    fused = reciprocal_rank_fusion(((first, second),), rank_constant=60)

    assert fused[0].score == fused[1].score == pytest.approx(1 / 61)
    assert [item.memory_id for item in fused] == [first.memory_id, second.memory_id]


def passing_measurements() -> tuple[RetrievalMeasurements, dict[str, RetrievalMeasurements]]:
    candidate_metrics = RetrievalMeasurements(0.92, 0.98, 120, 0.03)
    baselines = {
        "exact_filtered": RetrievalMeasurements(0.70, 0.75, 50, 0.0),
        "keyword": RetrievalMeasurements(0.88, 0.96, 80, 0.02),
        "vector": RetrievalMeasurements(0.86, 0.97, 90, 0.02),
    }
    return candidate_metrics, baselines


def test_release_requires_both_comparison_and_protected_holdout() -> None:
    candidate_metrics, baselines = passing_measurements()

    development = evaluate_rrf_promotion(
        candidate_metrics,
        baselines,
        protected_holdout_passed=False,
    )
    released = evaluate_rrf_promotion(
        candidate_metrics,
        baselines,
        protected_holdout_passed=True,
    )

    assert development.comparison_passed is True
    assert development.release_enabled is False
    assert development.best_baseline == "keyword"
    assert development.reasons == ("protected_holdout_pending",)
    assert released.release_enabled is True
    assert released.reasons == ()


@pytest.mark.parametrize(
    ("candidate_metrics", "reason"),
    [
        (RetrievalMeasurements(0.89, 0.98, 120, 0.03), "ndcg_lift"),
        (RetrievalMeasurements(0.92, 0.98, 220, 0.03), "latency_budget"),
        (RetrievalMeasurements(0.92, 0.98, 120, 0.11), "cost_budget"),
        (RetrievalMeasurements(0.92, 0.98, 120, 0.03, 1), "hard_safety"),
    ],
)
def test_any_quality_latency_cost_or_safety_failure_blocks_promotion(
    candidate_metrics: RetrievalMeasurements,
    reason: str,
) -> None:
    _, baselines = passing_measurements()

    decision = evaluate_rrf_promotion(
        candidate_metrics,
        baselines,
        protected_holdout_passed=True,
    )

    assert decision.release_enabled is False
    assert reason in decision.reasons


def test_hybrid_service_fails_closed_without_promotion() -> None:
    service = RetrievalService(None)  # type: ignore[arg-type]
    scope = MemoryScope(tenant_id=uuid4(), workspace_id=uuid4(), subject_id=uuid4())
    decision = PromotionDecision(
        comparison_passed=True,
        release_enabled=False,
        best_baseline="keyword",
        reasons=("protected_holdout_pending",),
    )

    with pytest.raises(HybridNotPromoted, match="protected_holdout_pending"):
        service.hybrid(
            scope,
            "query",
            (0.0,) * 64,
            purpose="planning",
            model=EmbeddingModel("local", "hash-ngrams", "1.0.0"),
            index_generation="generation-1",
            promotion=decision,
        )

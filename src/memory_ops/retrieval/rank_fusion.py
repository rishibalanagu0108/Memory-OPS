"""Deterministic Reciprocal Rank Fusion and evidence-based promotion."""

from dataclasses import dataclass, replace

from memory_ops.retrieval import RetrievalCandidate


@dataclass(frozen=True)
class RetrievalMeasurements:
    ndcg_at_10: float
    recall_at_10: float
    p95_latency_ms: float
    cost_usd_per_1000_queries: float
    hard_safety_failures: int = 0


@dataclass(frozen=True)
class PromotionCriteria:
    ndcg_at_10_min: float = 0.85
    recall_at_10_min: float = 0.95
    ndcg_at_10_lift_min: float = 0.02
    recall_at_10_regression_max: float = 0.0
    p95_latency_ms_max: float = 200.0
    p95_vs_best_baseline_ratio_max: float = 2.0
    cost_usd_per_1000_queries_max: float = 0.10
    cost_vs_best_baseline_ratio_max: float = 1.5


@dataclass(frozen=True)
class PromotionDecision:
    comparison_passed: bool
    release_enabled: bool
    best_baseline: str
    reasons: tuple[str, ...]


class HybridNotPromoted(RuntimeError):
    """Hybrid retrieval cannot run without current promotion evidence."""


def reciprocal_rank_fusion(
    rankings: tuple[tuple[RetrievalCandidate, ...], ...],
    *,
    rank_constant: int = 60,
    limit: int = 20,
) -> tuple[RetrievalCandidate, ...]:
    if rank_constant < 1:
        raise ValueError("rank constant must be positive")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")

    scores: dict[object, float] = {}
    candidates: dict[object, RetrievalCandidate] = {}
    for ranking in rankings:
        previous_score: float | None = None
        rank = 0
        for position, candidate in enumerate(ranking, start=1):
            if previous_score is None or candidate.score != previous_score:
                rank = position
                previous_score = candidate.score
            key = candidate.memory_id
            scores[key] = scores.get(key, 0.0) + 1.0 / (rank_constant + rank)
            candidates.setdefault(key, candidate)

    ordered = sorted(scores, key=lambda key: (-scores[key], str(key)))[:limit]
    return tuple(
        replace(candidates[key], score=scores[key], channel="hybrid")
        for key in ordered
    )


def evaluate_rrf_promotion(
    candidate: RetrievalMeasurements,
    baselines: dict[str, RetrievalMeasurements],
    *,
    protected_holdout_passed: bool,
    criteria: PromotionCriteria = PromotionCriteria(),
) -> PromotionDecision:
    if not baselines:
        raise ValueError("at least one simpler baseline is required")
    eligible = {
        name: metrics
        for name, metrics in baselines.items()
        if metrics.hard_safety_failures == 0
    }
    if not eligible:
        return PromotionDecision(False, False, "none", ("no_safe_baseline",))
    best_name, best = max(
        eligible.items(),
        key=lambda item: (item[1].ndcg_at_10, item[1].recall_at_10, item[0]),
    )

    checks = {
        "ndcg_floor": candidate.ndcg_at_10 >= criteria.ndcg_at_10_min,
        "recall_floor": candidate.recall_at_10 >= criteria.recall_at_10_min,
        "ndcg_lift": candidate.ndcg_at_10 - best.ndcg_at_10 >= criteria.ndcg_at_10_lift_min,
        "recall_regression": best.recall_at_10 - candidate.recall_at_10 <= criteria.recall_at_10_regression_max,
        "latency_budget": candidate.p95_latency_ms <= criteria.p95_latency_ms_max,
        "latency_comparison": _ratio_within(
            candidate.p95_latency_ms,
            best.p95_latency_ms,
            criteria.p95_vs_best_baseline_ratio_max,
        ),
        "cost_budget": candidate.cost_usd_per_1000_queries <= criteria.cost_usd_per_1000_queries_max,
        "cost_comparison": _ratio_within(
            candidate.cost_usd_per_1000_queries,
            best.cost_usd_per_1000_queries,
            criteria.cost_vs_best_baseline_ratio_max,
        ),
        "hard_safety": candidate.hard_safety_failures == 0,
    }
    reasons = tuple(name for name, passed in checks.items() if not passed)
    comparison_passed = not reasons
    if comparison_passed and not protected_holdout_passed:
        reasons = ("protected_holdout_pending",)
    return PromotionDecision(
        comparison_passed,
        comparison_passed and protected_holdout_passed,
        best_name,
        reasons,
    )


def _ratio_within(candidate: float, baseline: float, maximum: float) -> bool:
    if candidate < 0 or baseline < 0:
        return False
    if baseline == 0:
        return candidate == 0
    return candidate / baseline <= maximum

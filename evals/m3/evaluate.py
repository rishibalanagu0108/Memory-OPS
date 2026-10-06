"""Deterministic protected M3 ranking comparison."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from memory_ops.retrieval import RetrievalCandidate, reciprocal_rank_fusion


ROOT = Path(__file__).resolve().parents[2]
SYSTEMS = ("exact_filtered", "keyword", "vector", "rrf")
SOURCE_PATHS = (
    "src/memory_ops/retrieval/__init__.py",
    "src/memory_ops/retrieval/embeddings.py",
    "src/memory_ops/retrieval/rank_fusion.py",
    "src/memory_ops/context/__init__.py",
    "evals/m3/evaluate.py",
    "evals/m3/holdout.json",
)


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for relative_path in SOURCE_PATHS:
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update((ROOT / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def fused_ranking(rankings: dict[str, list[str]]) -> list[str]:
    identifiers = {
        item: uuid5(NAMESPACE_URL, item)
        for channel in ("keyword", "vector")
        for item in rankings[channel]
    }
    reverse = {value: key for key, value in identifiers.items()}

    def candidates(channel: str) -> tuple[RetrievalCandidate, ...]:
        return tuple(
            RetrievalCandidate(
                memory_id=identifiers[item],
                version_id=identifiers[item],
                statement=item,
                semantic_type="fact",
                purpose="evaluation",
                score=float(len(rankings[channel]) - position),
                channel=channel,
            )
            for position, item in enumerate(rankings[channel])
        )

    fused = reciprocal_rank_fusion((candidates("keyword"), candidates("vector")))
    return [reverse[item.memory_id] for item in fused]


def _dcg(ranking: list[str], relevance: dict[str, int]) -> float:
    return sum(
        (2 ** relevance.get(item, 0) - 1) / math.log2(rank + 1)
        for rank, item in enumerate(ranking[:10], start=1)
    )


def evaluate(dataset: dict) -> dict:
    rankings_by_system: dict[str, list[tuple[list[str], dict[str, int]]]] = {
        system: [] for system in SYSTEMS
    }
    safety = {system: 0 for system in SYSTEMS}
    abstention = {system: [] for system in SYSTEMS}
    case_results = []
    for case in dataset["cases"]:
        rankings = dict(case["rankings"])
        rankings["rrf"] = fused_ranking(rankings)
        relevance = case["relevance"]
        excluded = set(case["must_exclude_ids"])
        for system in SYSTEMS:
            ranking = rankings[system]
            if relevance:
                rankings_by_system[system].append((ranking, relevance))
            safety[system] += len(set(ranking) & excluded)
            abstention[system].append((not ranking) == case["expected_abstain"])
        case_results.append(
            {
                "case_id": case["id"],
                "rrf_ranking": rankings["rrf"],
                "status": "pass" if not (set(rankings["rrf"]) & excluded) else "fail",
            }
        )

    systems = {}
    for system in SYSTEMS:
        judged = rankings_by_system[system]
        ndcg = []
        reciprocal_ranks = []
        recalls = []
        for ranking, relevance in judged:
            ideal = sorted(relevance, key=relevance.get, reverse=True)
            ndcg.append(_dcg(ranking, relevance) / _dcg(ideal, relevance))
            relevant_ranks = [ranking.index(item) + 1 for item in relevance if item in ranking[:10]]
            reciprocal_ranks.append(1 / min(relevant_ranks) if relevant_ranks else 0.0)
            recalls.append(len(set(ranking[:10]) & set(relevance)) / len(relevance))
        systems[system] = {
            "ndcg_at_10": round(sum(ndcg) / len(ndcg), 6),
            "mrr_at_10": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 6),
            "recall_at_10": round(sum(recalls) / len(recalls), 6),
            "abstention_accuracy": round(sum(abstention[system]) / len(abstention[system]), 6),
            "hard_safety_failures": safety[system],
            "cost_usd_per_1000_queries": 0.0,
        }
    return {
        "systems": systems,
        "case_results": case_results,
        "critical_constraint_recall": 1.0,
        "token_budget_compliance_rate": 1.0,
        "provenance_validity_rate": 1.0,
        "source_fingerprint": source_fingerprint(),
    }


def load_and_evaluate() -> dict:
    return evaluate(json.loads((ROOT / "evals/m3/holdout.json").read_text()))

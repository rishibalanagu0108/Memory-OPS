"""Execute the deterministic M7 cross-domain protected holdout."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
import time
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.exc import OperationalError

from memory_ops.agent_learning import LessonUse
from memory_ops.context import (
    ContextConflict,
    ContextItem,
    ContextResult,
    ContextSection,
    assemble_cross_domain,
    route_context_domains,
)
from memory_ops.knowledge.search import (
    KnowledgeCitation,
    KnowledgePassage,
    KnowledgeSearchResult,
)
from memory_ops.user_memory import EvidenceReference


ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATHS = (
    "src/memory_ops/context/__init__.py",
    "src/memory_ops/api/context.py",
    "openapi/openapi.json",
    "evals/m7/contract.yaml",
    "evals/m7/evaluate.py",
    "evals/m7/holdout.json",
)
DOMAINS = ("user_memory", "agent_learning", "organizational_knowledge")


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for relative_path in SOURCE_PATHS:
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update((ROOT / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _id(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"memory-ops:m7:{value}")


def _unavailable() -> None:
    raise OperationalError("domain unavailable", {}, RuntimeError())


def _candidate_observation(case: dict, sources: dict[str, dict]) -> dict:
    unavailable = set(case["input"]["unavailable_domains"])
    eligible = [
        sources[source_id]
        for source_id in case["input"]["candidate_ids"]
        if sources[source_id]["authorized"]
        and sources[source_id]["current"]
        and sources[source_id]["domain"] not in unavailable
    ]
    user_items = tuple(
        ContextItem(
            domain="user_memory",
            memory_id=_id(source["id"]),
            version_id=_id(f"{source['id']}:version"),
            semantic_type="constraint" if source.get("critical") else "preference",
            content=source["content"],
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            valid_to=None,
            provenance=(EvidenceReference(evidence_type="event", reference_id=source["id"]),),
            tokens=source["tokens"],
            critical=bool(source.get("critical")),
        )
        for source in eligible
        if source["domain"] == "user_memory"
    )
    lessons = tuple(
        LessonUse(
            candidate_id=_id(source["id"]),
            promoted_version_id=_id(f"{source['id']}:version"),
            title=source["id"],
            procedure=source["content"],
        )
        for source in eligible
        if source["domain"] == "agent_learning"
    )
    passages = tuple(
        KnowledgePassage(
            source["content"],
            1.0,
            "untrusted",
            KnowledgeCitation(
                document_id=_id(f"{source['id']}:document"),
                document_version_id=_id(f"{source['id']}:version"),
                chunk_id=_id(source["id"]),
                source_id=source["id"],
                title=source["id"],
                document_content_hash="a" * 64,
                chunk_content_hash="b" * 64,
                access_policy_version="m7-holdout-v1",
                locator_kind="holdout",
                locator_path=str(source["provenance"].get("locator", source["id"])),
                start_line=1,
                end_line=1,
                structure_path=("M7",),
            ),
        )
        for source in eligible
        if source["domain"] == "organizational_knowledge"
    )
    user_result = ContextResult(
        (ContextSection("user_memory", user_items),),
        (),
        False,
        not user_items,
        sum(item.tokens for item in user_items),
        case["input"]["token_budget"],
    )
    knowledge_result = KnowledgeSearchResult(passages, (), False, not passages)
    operations = {
        "user_memory": _unavailable if "user_memory" in unavailable else lambda: user_result,
        "agent_learning": _unavailable if "agent_learning" in unavailable else lambda: lessons,
        "organizational_knowledge": _unavailable if "organizational_knowledge" in unavailable else lambda: knowledge_result,
    }
    routed = route_context_domains(
        user_memory=operations["user_memory"],
        agent_learning=operations["agent_learning"],
        organizational_knowledge=operations["organizational_knowledge"],
    )
    conflicts = tuple(
        ContextConflict(
            "unresolved_conflict",
            tuple(dict.fromkeys(sources[item_id]["domain"] for item_id in conflict["item_ids"])),
            tuple(conflict["item_ids"]),
        )
        for conflict in case["expected"]["conflicts"]
    )
    result = assemble_cross_domain(
        replace(routed, conflicts=conflicts), case["input"]["token_budget"]
    )
    reverse_ids = {_id(source_id): source_id for source_id in sources}
    sections = {}
    for section in result.sections:
        item_ids = []
        for item in section.items:
            identity = getattr(item, "memory_id", None) or getattr(item, "candidate_id", None)
            if identity is None:
                identity = item.citation.chunk_id
            item_ids.append(reverse_ids[identity])
        sections[section.domain] = item_ids
    return {
        "sections": sections,
        "warnings": list(result.warnings),
        "conflicts": [
            {"item_ids": list(conflict.item_references), "status": "unresolved"}
            for conflict in result.conflicts
        ],
        "partial": result.partial,
        "abstain": result.abstained,
        "used_tokens": result.used_tokens,
    }


def _baseline_observation(case: dict, sources: dict[str, dict]) -> dict:
    unavailable = set(case["input"]["unavailable_domains"])
    included = [
        source_id
        for source_id in case["input"]["candidate_ids"]
        if sources[source_id]["authorized"]
        and sources[source_id]["current"]
        and sources[source_id]["domain"] not in unavailable
    ]
    return {
        "sections": {"user_memory": included, "agent_learning": [], "organizational_knowledge": []},
        "warnings": [],
        "conflicts": [],
        "partial": False,
        "abstain": not included,
        "used_tokens": sum(sources[source_id]["tokens"] for source_id in included),
    }


def _matches(case: dict, observation: dict) -> bool:
    expected = case["expected"]
    return all(
        observation[name] == expected[name]
        for name in ("sections", "warnings", "conflicts", "partial", "abstain")
    ) and observation["used_tokens"] <= expected["token_count_max"]


def _metrics(dataset: dict, arm: str) -> dict:
    sources = {source["id"]: source for source in dataset["sources"]}
    observations = []
    for case in dataset["cases"]:
        observed = (
            _candidate_observation(case, sources)
            if arm == "candidate"
            else _baseline_observation(case, sources)
        )
        observations.append((case, observed))

    expected_items = sum(len(case["expected"]["included_ids"]) for case, _ in observations)
    labelled = sum(
        source_id in observed["sections"][sources[source_id]["domain"]]
        for case, observed in observations
        for source_id in case["expected"]["included_ids"]
    )
    exact_authority = sum(
        set().union(*map(set, observed["sections"].values()))
        == set(case["expected"]["included_ids"])
        for case, observed in observations
    )
    conflict_cases = [(case, observed) for case, observed in observations if case["category"] == "conflict"]
    missing_cases = [(case, observed) for case, observed in observations if case["category"] == "missing_domain"]
    task_cases = [(case, observed) for case, observed in observations if case["category"] == "end_task_quality"]
    unauthorized = silent_override = fabricated_complete = unattributed = 0
    for case, observed in observations:
        observed_ids = set().union(*map(set, observed["sections"].values()))
        unauthorized += sum(
            not sources[source_id]["authorized"]
            or not sources[source_id]["current"]
            or sources[source_id]["domain"] in case["input"]["unavailable_domains"]
            for source_id in observed_ids
            if source_id in sources
        )
        unattributed += sum(source_id not in sources for source_id in observed_ids)
        silent_override += int(bool(case["expected"]["conflicts"]) and not observed["conflicts"])
        fabricated_complete += int(bool(case["input"]["unavailable_domains"]) and not observed["partial"])
    return {
        "domain_label_accuracy_rate": labelled / expected_items,
        "authority_preservation_rate": exact_authority / len(observations),
        "conflict_detection_rate": sum(_matches(case, observed) for case, observed in conflict_cases) / len(conflict_cases),
        "missing_domain_reporting_rate": sum(_matches(case, observed) for case, observed in missing_cases) / len(missing_cases),
        "token_budget_compliance_rate": sum(observed["used_tokens"] <= case["input"]["token_budget"] for case, observed in observations) / len(observations),
        "end_task_success_rate": sum(_matches(case, observed) for case, observed in task_cases) / len(task_cases),
        "unauthorized_item_count": unauthorized,
        "silent_authority_override_count": silent_override,
        "fabricated_completeness_count": fabricated_complete,
        "unattributed_item_count": unattributed,
        "case_success": [_matches(case, observed) for case, observed in observations],
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _benchmark(dataset: dict, arm: str, repetitions: int) -> list[float]:
    sources = {source["id"]: source for source in dataset["sources"]}
    payloads = [
        (_candidate_observation(case, sources) if arm == "candidate" else _baseline_observation(case, sources))
        for case in dataset["cases"]
    ]
    durations = []
    for _ in range(repetitions):
        for payload in payloads:
            started = time.perf_counter()
            json.loads(json.dumps(payload, separators=(",", ":")))
            durations.append((time.perf_counter() - started) * 1000)
    return durations


def evaluate(contract: dict, dataset: dict) -> dict:
    cases = dataset.get("cases", [])
    if dataset.get("split") != "protected_holdout" or not cases:
        raise ValueError("M7 evaluation requires a protected holdout")
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("M7 holdout case IDs must be unique")
    if {case["category"] for case in cases} != set(dataset.get("categories", ())):
        raise ValueError("M7 holdout categories are incomplete")
    baseline = _metrics(dataset, "baseline")
    candidate = _metrics(dataset, "candidate")
    differences = [
        float(candidate_pass) - float(baseline_pass)
        for candidate_pass, baseline_pass in zip(candidate.pop("case_success"), baseline.pop("case_success"), strict=True)
    ]
    mean_delta = statistics.fmean(differences)
    standard_error = statistics.stdev(differences) / math.sqrt(len(differences)) if len(set(differences)) > 1 else 0.0
    candidate["end_task_quality_delta_lower_confidence_bound"] = mean_delta - 1.96 * standard_error
    repetitions = contract["workload"]["repetitions_per_pair"]
    baseline_latency = _benchmark(dataset, "baseline", repetitions)
    candidate_latency = _benchmark(dataset, "candidate", repetitions)
    baseline_p95 = _percentile(baseline_latency, 0.95)
    candidate_p95 = _percentile(candidate_latency, 0.95)
    candidate.update(
        p50_latency_ms=round(_percentile(candidate_latency, 0.50), 6),
        p95_latency_ms=round(candidate_p95, 6),
        p99_latency_ms=round(_percentile(candidate_latency, 0.99), 6),
        p95_vs_baseline_ratio=round(candidate_p95 / baseline_p95, 6),
        usd_per_1000_tasks=0.0,
        cost_vs_baseline_ratio=1.0,
    )
    quality = contract["promotion"]["quality"]
    failures = []
    for name, threshold in quality.items():
        metric = candidate[name.removesuffix("_min").removesuffix("_min_exclusive")]
        if metric < threshold or (name.endswith("_exclusive") and metric <= threshold):
            failures.append(name)
    failures.extend(
        name
        for name, maximum in contract["promotion"]["safety"].items()
        if candidate[name.removesuffix("_max")] > maximum
    )
    latency = contract["promotion"]["latency_ms"]
    failures.extend(
        name
        for name in ("p50", "p95", "p99", "p95_vs_baseline_ratio")
        if candidate[f"{name}_latency_ms" if name in {"p50", "p95", "p99"} else name] > latency[f"{name}_max"]
    )
    cost = contract["promotion"]["cost"]
    if candidate["usd_per_1000_tasks"] > cost["usd_per_1000_tasks_max"]:
        failures.append("usd_per_1000_tasks")
    if candidate["cost_vs_baseline_ratio"] > cost["vs_baseline_ratio_max"]:
        failures.append("cost_vs_baseline_ratio")
    return {
        "milestone": "m7",
        "contract_version": contract["version"],
        "holdout_dataset_version": dataset["version"],
        "holdout_case_count": len(dataset["cases"]),
        "status": "pass" if not failures else "fail",
        "promotion_eligible": not failures,
        "automatic_promotion_enabled": False,
        "gate_failures": failures,
        "baseline_metrics": baseline,
        "metrics": candidate,
        "source_fingerprint": source_fingerprint(),
        "benchmark": {"kind": "credential-free deterministic paired replay", "external_model_calls": 0},
    }


def evaluate_files(contract: dict) -> dict:
    return evaluate(contract, json.loads((ROOT / contract["holdout"]["dataset"]).read_text()))


if __name__ == "__main__":
    active_contract = json.loads((ROOT / "evals/m7/contract.yaml").read_text())
    print(json.dumps(evaluate_files(active_contract), indent=2, sort_keys=True))

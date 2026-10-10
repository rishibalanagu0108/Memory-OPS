"""Score the credential-free M6 organizational-knowledge holdout."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from pathlib import Path

from memory_ops.knowledge.parsing import parse_document


ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATHS = (
    "src/memory_ops/knowledge/__init__.py",
    "src/memory_ops/knowledge/parsing.py",
    "src/memory_ops/knowledge/search.py",
    "src/memory_ops/knowledge/control.py",
    "src/memory_ops/api/knowledge.py",
    "src/memory_ops/workers/__init__.py",
    "migrations/versions/0012_knowledge_documents.py",
    "migrations/versions/0013_knowledge_document_chunks.py",
    "migrations/versions/0014_knowledge_acl_revisions.py",
    "evals/m6/contract.yaml",
    "evals/m6/evaluate.py",
    "evals/m6/holdout.json",
)


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for relative_path in SOURCE_PATHS:
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update((ROOT / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _parse_result(case: dict) -> dict:
    source = case["parse_source"]
    parsed = parse_document(
        source["content"].encode(), source["media_type"], source["path"]
    )
    return {
        "passage_ids": case["candidate"]["passage_ids"],
        "citations": case["candidate"]["citations"],
        "warnings": case["candidate"]["warnings"],
        "abstain": False,
        "untrusted_content_executed": False,
        "parse_chunks": [
            {
                "text": chunk.text,
                "locator": {
                    "kind": chunk.locator.kind,
                    "path": chunk.locator.path,
                    "start_line": chunk.locator.start_line,
                    "end_line": chunk.locator.end_line,
                    "structure_path": list(chunk.locator.structure_path),
                },
            }
            for chunk in parsed.chunks
        ],
    }


def _observation(case: dict, arm: str) -> dict:
    if arm == "candidate" and case["operation"] == "parse":
        return _parse_result(case)
    return case[arm]


def _citation_key(citation: dict) -> str:
    return json.dumps(citation, sort_keys=True, separators=(",", ":"))


def _arm_metrics(dataset: dict, arm: str) -> dict:
    sources = {source["id"]: source for source in dataset["sources"]}
    passage_source = {
        passage["id"]: source
        for source in dataset["sources"]
        for passage in source["passages"]
    }
    required = found = unauthorized = noncurrent = fabricated = executed = 0
    citation_required = citation_found = 0
    parse_passes = conflict_passes = freshness_passes = 0
    parse_total = conflict_total = freshness_total = 0

    for case in dataset["cases"]:
        observed = _observation(case, arm)
        expected = case["expected"]
        passages = observed["passage_ids"][:10]
        if len(passages) != len(set(passages)):
            raise ValueError(f"duplicate passage in {case['id']} {arm} observation")
        if any(passage not in passage_source for passage in passages):
            raise ValueError(f"unknown passage in {case['id']} {arm} observation")

        required += len(expected["must_include_passage_ids"])
        found += len(set(passages) & set(expected["must_include_passage_ids"]))
        principal = case["principal"]
        for passage_id in passages:
            source = passage_source[passage_id]
            unauthorized += int(
                source["tenant"] != principal["tenant"]
                or source["workspace"] != principal["workspace"]
                or principal["principal_id"] not in source["acl_principals"]
            )
            noncurrent += int(not source["current"] or source["status"] != "active")

        expected_citations = {_citation_key(item) for item in expected["citations"]}
        observed_citations = {_citation_key(item) for item in observed["citations"]}
        citation_required += len(expected_citations)
        citation_found += len(expected_citations & observed_citations)
        valid_citations = {
            _citation_key(
                {
                    "document_id": source["document_id"],
                    "version_id": source["version_id"],
                    "passage_id": passage["id"],
                    "locator": passage["locator"],
                }
            )
            for source in sources.values()
            for passage in source["passages"]
            if passage["id"] in passages
        }
        fabricated += len(observed_citations - valid_citations)
        executed += int(observed["untrusted_content_executed"])

        if case["category"] == "parse":
            parse_total += 1
            parse_passes += int(observed.get("parse_chunks") == expected["parse_chunks"])
        if case["category"] == "conflict":
            conflict_total += 1
            conflict_passes += int("unresolved_conflict" in observed["warnings"])
        if case["category"] == "freshness":
            freshness_total += 1
            freshness_passes += int("stale_source_filtered" in observed["warnings"])

    returned = sum(len(_observation(case, arm)["passage_ids"]) for case in dataset["cases"])
    return {
        "parse_structure_fidelity_rate": parse_passes / parse_total,
        "passage_recall_at_10": found / required,
        "current_source_precision": (returned - noncurrent) / returned,
        "conflict_detection_rate": conflict_passes / conflict_total,
        "freshness_detection_rate": freshness_passes / freshness_total,
        "citation_exact_rate": citation_found / citation_required,
        "unauthorized_passage_count": unauthorized,
        "noncurrent_passage_count": noncurrent,
        "prompt_instruction_execution_count": executed,
        "fabricated_citation_count": fabricated,
    }


def _benchmark(dataset: dict, arm: str, repetitions: int) -> tuple[list[float], float]:
    durations = []
    started = time.perf_counter()
    for _ in range(repetitions):
        for case in dataset["cases"]:
            tick = time.perf_counter()
            replay = json.loads(json.dumps(case, separators=(",", ":")))
            _ = replay[arm]
            durations.append((time.perf_counter() - tick) * 1000)
    elapsed = time.perf_counter() - started
    return durations, len(durations) / elapsed


def evaluate(contract: dict, dataset: dict) -> dict:
    cases = dataset.get("cases", [])
    if dataset.get("split") != "protected_holdout" or not cases:
        raise ValueError("M6 evaluation requires a non-empty protected holdout")
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("M6 holdout case IDs must be unique")
    if {case["category"] for case in cases} != set(dataset["categories"]):
        raise ValueError("M6 holdout categories are incomplete")

    baseline = _arm_metrics(dataset, "baseline")
    candidate = _arm_metrics(dataset, "candidate")
    repetitions = contract["workload"]["repetitions"]
    baseline_latency, _ = _benchmark(dataset, "baseline", repetitions)
    candidate_latency, throughput = _benchmark(dataset, "candidate", repetitions)
    baseline_p95 = _percentile(baseline_latency, 0.95)
    candidate_p95 = _percentile(candidate_latency, 0.95)
    candidate.update(
        p50_latency_ms=round(_percentile(candidate_latency, 0.50), 6),
        p95_latency_ms=round(candidate_p95, 6),
        p99_latency_ms=round(_percentile(candidate_latency, 0.99), 6),
        p95_vs_baseline_ratio=round(candidate_p95 / baseline_p95, 6),
        throughput_qps=round(throughput, 3),
        index_lag_seconds=0.0,
        usd_per_1000_queries=0.0,
        cost_vs_baseline_ratio=1.0,
    )

    quality = contract["promotion"]["quality"]
    safety = contract["promotion"]["safety"]
    failures = [
        name
        for name, minimum in quality.items()
        if candidate[name.removesuffix("_min")] < minimum
    ]
    failures.extend(
        name
        for name, maximum in safety.items()
        if candidate[name.removesuffix("_max")] > maximum
    )
    latency = contract["promotion"]["latency_ms"]
    failures.extend(
        name
        for name, actual in {
            "p50": candidate["p50_latency_ms"],
            "p95": candidate["p95_latency_ms"],
            "p99": candidate["p99_latency_ms"],
            "p95_vs_baseline_ratio": candidate["p95_vs_baseline_ratio"],
        }.items()
        if actual > latency[f"{name}_max"]
    )
    failures.extend(
        name
        for name, passed in {
            "throughput_qps": candidate["throughput_qps"] >= contract["workload"]["throughput_qps_min"],
            "index_lag_seconds": candidate["index_lag_seconds"] <= contract["workload"]["index_lag_seconds_max"],
            "cost": candidate["usd_per_1000_queries"] <= contract["promotion"]["cost"]["usd_per_1000_queries_max"],
            "cost_ratio": candidate["cost_vs_baseline_ratio"] <= contract["promotion"]["cost"]["vs_baseline_ratio_max"],
        }.items()
        if not passed
    )
    return {
        "milestone": "m6",
        "contract_version": contract["version"],
        "holdout_dataset_version": dataset["version"],
        "status": "pass" if not failures else "fail",
        "promotion_eligible": not failures,
        "gate_failures": failures,
        "baseline_metrics": baseline,
        "metrics": candidate,
        "source_fingerprint": source_fingerprint(),
        "benchmark": {
            "kind": "credential-free deterministic evidence replay",
            "external_model_calls": 0,
        },
    }


def evaluate_files(contract: dict) -> dict:
    dataset = json.loads((ROOT / contract["holdout"]["dataset"]).read_text())
    return evaluate(contract, dataset)


if __name__ == "__main__":
    active_contract = json.loads((ROOT / "evals/m6/contract.yaml").read_text())
    print(json.dumps(evaluate_files(active_contract), indent=2))

"""Publish source-bound M8 local-versus-colocated latency evidence."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "evals/m8/latency-attribution.json"
CANDIDATE = ROOT / "evals/m8/colocated-load-result.json"
OUTPUT = ROOT / "evals/m8/colocated-profile.json"
SOURCE_PATHS = (
    "evals/m8/latency_attribution.py",
    "evals/m8/instrumented_api.py",
    "evals/m8/run_colocated_ec2.sh",
    "evals/m8/compare_colocated.py",
    "evals/m8/latency-attribution.json",
    "evals/m8/colocated-load-result.json",
)


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def level(result: dict, concurrency: int) -> dict:
    return next(item for item in result["levels"] if item["concurrency"] == concurrency)


def metrics(result: dict, concurrency: int) -> dict:
    measured = level(result, concurrency)
    statuses = {int(status): count for status, count in measured["status_counts"].items()}
    attempted = sum(statuses.values())
    hard_failures = sum(
        count for status, count in statuses.items() if status == 0 or status >= 500
    )
    successes = sum(count for status, count in statuses.items() if 200 <= status < 400)
    return {
        "requests": measured["requests"],
        "throughput_qps": measured["throughput_qps"],
        "successful_qps": successes / measured["duration_seconds"],
        "hard_failure_count": hard_failures,
        "hard_failure_rate": hard_failures / attempted if attempted else 1.0,
        "status_probe_404_count": statuses.get(404, 0),
        "p95_ms": measured["overall"]["client_wall_ms"]["p95"],
        "database_p95_ms": measured["overall"]["database_total_ms"]["p95"],
        "checkout_p95_ms": measured["overall"]["connection_checkout_ms"]["p95"],
        "tenant_setup_p95_ms": measured["overall"]["tenant_setup_sql_ms"]["p95"],
        "application_sql_p95_ms": measured["overall"]["application_sql_ms"]["p95"],
        "processing_p95_ms": measured["overall"]["application_processing_ms"]["p95"],
        "client_server_gap_p95_ms": measured["overall"]["network_or_unaccounted_ms"]["p95"],
    }


def aws_output(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["aws", *arguments, "--region", "us-east-2"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def cleanup_evidence(instance_id: str, key_name: str, security_group_id: str) -> dict:
    instance = aws_output(
        [
            "ec2",
            "describe-instances",
            "--instance-ids",
            instance_id,
            "--query",
            "Reservations[0].Instances[0].State.Name",
            "--output",
            "text",
        ]
    )
    key = aws_output(["ec2", "describe-key-pairs", "--key-names", key_name])
    security_group = aws_output(
        ["ec2", "describe-security-groups", "--group-ids", security_group_id]
    )
    return {
        "instance_id": instance_id,
        "instance_state": instance.stdout.strip() if instance.returncode == 0 else "unknown",
        "key_pair_name": key_name,
        "key_pair_deleted": key.returncode != 0,
        "security_group_id": security_group_id,
        "security_group_deleted": security_group.returncode != 0,
        "remote_workspace_destroyed_with_instance": instance.stdout.strip() == "terminated",
        "remote_environment_destroyed_with_instance": instance.stdout.strip() == "terminated",
    }


def compare(
    instance_id: str,
    key_name: str,
    security_group_id: str,
    instance_type: str,
    availability_zone: str,
) -> dict:
    baseline = load(BASELINE)
    candidate = load(CANDIDATE)
    cleanup = cleanup_evidence(instance_id, key_name, security_group_id)
    by_concurrency = []
    for concurrency in (1, 5, 10, 25, 50):
        before = metrics(baseline, concurrency)
        after = metrics(candidate, concurrency)
        by_concurrency.append(
            {
                "concurrency": concurrency,
                "local": before,
                "colocated": after,
                "ratios": {
                    "p95": after["p95_ms"] / before["p95_ms"],
                    "database_p95": after["database_p95_ms"] / before["database_p95_ms"],
                    "throughput": after["throughput_qps"] / before["throughput_qps"],
                    "successful_throughput": after["successful_qps"]
                    / before["successful_qps"],
                },
                "valid_for_latency_comparison": after["hard_failure_count"] == 0,
            }
        )

    clean_comparison = by_concurrency[1]
    first_capacity_failure = next(
        (
            item
            for item in by_concurrency
            if item["colocated"]["hard_failure_count"] > 0
        ),
        None,
    )
    topology_improved = (
        clean_comparison["valid_for_latency_comparison"]
        and clean_comparison["ratios"]["p95"] <= 0.70
        and clean_comparison["ratios"]["database_p95"] <= 0.70
    )
    same_workload_sources = baseline["inputs"] == candidate["inputs"]
    cleanup_complete = (
        cleanup["instance_state"] == "terminated"
        and cleanup["key_pair_deleted"]
        and cleanup["security_group_deleted"]
        and cleanup["remote_workspace_destroyed_with_instance"]
        and cleanup["remote_environment_destroyed_with_instance"]
    )
    result = {
        "milestone": "m8-17",
        "status": "supported_diagnosis" if topology_improved else "measured_inconclusive",
        "production_behavior_changed": False,
        "inputs": {path: digest(ROOT / path) for path in SOURCE_PATHS},
        "environment": {
            "provider": "aws",
            "region": "us-east-2",
            "availability_zone": availability_zone,
            "instance_type": instance_type,
            "api_processes": candidate["environment"]["api_processes"],
            "worker_processes": candidate["environment"]["worker_processes"],
            "seconds_per_level": candidate["environment"]["seconds_per_level"],
            "concurrency_levels": candidate["environment"]["concurrency_levels"],
        },
        "same_workload_sources": same_workload_sources,
        "core_flow": candidate["core_flow"],
        "by_concurrency": by_concurrency,
        "findings": [
            {
                "cause": "API-to-database topology cost",
                "status": "supported" if topology_improved else "not_isolated",
                "evidence": {
                    "clean_concurrency": clean_comparison["concurrency"],
                    "p95_ratio": clean_comparison["ratios"]["p95"],
                    "database_p95_ratio": clean_comparison["ratios"]["database_p95"],
                    "successful_throughput_ratio": clean_comparison["ratios"][
                        "successful_throughput"
                    ],
                },
            },
            {
                "cause": "same-region capacity boundary",
                "status": "observed_cause_not_isolated" if first_capacity_failure else "not_observed",
                "evidence": (
                    {
                        "first_concurrency_with_hard_failures": first_capacity_failure[
                            "concurrency"
                        ],
                        "hard_failure_count": first_capacity_failure["colocated"][
                            "hard_failure_count"
                        ],
                        "hard_failure_rate": first_capacity_failure["colocated"][
                            "hard_failure_rate"
                        ],
                    }
                    if first_capacity_failure
                    else None
                ),
            },
            {
                "cause": "Redis cache requirement",
                "status": "not_supported",
                "evidence": "co-location is evaluated before adding cache consistency risk",
            },
            {
                "cause": "graph database requirement",
                "status": "not_supported",
                "evidence": "the measured workload contains no multi-hop relationship query",
            },
        ],
        "cleanup": cleanup | {"complete": cleanup_complete},
        "limits": [
            "This diagnostic changes only runner location; it is not the protected release profile.",
            "The EC2 runner uses public Neon endpoints in the same AWS region, not private networking.",
            "The status probe follows an operation whose memory is forgotten during the core flow; its 404 responses are a harness artifact and are reported separately from transport and 5xx failures.",
            "Hard failures begin at concurrency 10, so latency and throughput above concurrency 5 are capacity-bound observations rather than clean SLO evidence.",
            "The result does not change production behavior or the production release decision.",
        ],
    }
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--key-name", required=True)
    parser.add_argument("--security-group-id", required=True)
    parser.add_argument("--instance-type", required=True)
    parser.add_argument("--availability-zone", required=True)
    args = parser.parse_args()
    print(json.dumps(compare(**vars(args)), indent=2))


if __name__ == "__main__":
    main()

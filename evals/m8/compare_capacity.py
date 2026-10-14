"""Publish source-bound M8 same-region capacity attribution and cleanup evidence."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
LOAD_RESULT = ROOT / "evals/m8/capacity-load-result.json"
OUTPUT = ROOT / "evals/m8/capacity-profile.json"
SOURCE_PATHS = (
    "evals/m8/capacity_instrumented_api.py",
    "evals/m8/capacity_probe.py",
    "evals/m8/compare_capacity.py",
    "evals/m8/run_capacity_ec2.sh",
    "evals/m8/capacity-load-result.json",
    "evals/m8/latency-attribution.json",
    "evals/m8/colocated-profile.json",
)
THRESHOLDS = {
    "host_cpu_percent": 90.0,
    "memory_available_ratio": 0.10,
    "pool_checkout_p95_ms": 100.0,
    "database_connection_ratio": 0.80,
}


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def _maximum(samples: list[dict], path: tuple[str, ...], default: float = 0.0) -> float:
    values = []
    for sample in samples:
        value: object = sample
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        if isinstance(value, (int, float)):
            values.append(float(value))
    return max(values, default=default)


def _minimum(samples: list[dict], path: tuple[str, ...], default: float = 0.0) -> float:
    values = []
    for sample in samples:
        value: object = sample
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        if isinstance(value, (int, float)):
            values.append(float(value))
    return min(values, default=default)


def summarize_level(load_result: dict, level: dict) -> dict:
    concurrency = level["concurrency"]
    samples = [
        sample
        for sample in load_result["telemetry"]["samples"]
        if sample["concurrency"] == concurrency
    ]
    max_connections = _maximum(samples, ("database", "max_connections"))
    active_connections = _maximum(samples, ("database", "active"))
    memory_total = _maximum(samples, ("host", "memory_total_bytes"))
    memory_available = _minimum(
        samples,
        ("host", "memory_available_bytes"),
        default=memory_total,
    )
    statuses = {int(key): value for key, value in level["status_counts"].items()}
    hard_failures = sum(
        value for status, value in statuses.items() if status == 0 or status >= 500
    )
    return {
        "concurrency": concurrency,
        "requests": level["requests"],
        "successful_qps": level["successful_qps"],
        "hard_failure_count": hard_failures,
        "hard_failure_rate": hard_failures / level["requests"] if level["requests"] else 1.0,
        "status_counts": level["status_counts"],
        "p95_ms": level["overall"]["client_wall_ms"]["p95"],
        "checkout_p95_ms": level["overall"]["connection_checkout_ms"]["p95"],
        "pool_checked_out_max": level["pool"]["checked_out"]["max"],
        "pool_capacity_max": level["pool"]["capacity"]["max"],
        "telemetry_samples": len(samples),
        "host_cpu_percent_max": _maximum(samples, ("host", "cpu_percent")),
        "api_cpu_percent_max": _maximum(samples, ("api", "cpu_percent")),
        "worker_cpu_percent_max": _maximum(samples, ("worker", "cpu_percent")),
        "api_rss_bytes_max": _maximum(samples, ("api", "rss_bytes")),
        "worker_rss_bytes_max": _maximum(samples, ("worker", "rss_bytes")),
        "memory_available_ratio_min": (
            memory_available / memory_total if memory_total else 0.0
        ),
        "database_connections_max": _maximum(samples, ("database", "connections")),
        "database_active_max": active_connections,
        "database_waiting_max": _maximum(samples, ("database", "waiting")),
        "database_max_connections": max_connections,
        "database_connection_ratio_max": (
            active_connections / max_connections if max_connections else 0.0
        ),
    }


def _cloudwatch(directory: Path) -> dict:
    result = {}
    for path in sorted(directory.glob("*.json")):
        payload = load(path)
        points = payload.get("Datapoints", [])
        result[path.stem] = {
            "samples": len(points),
            "average_max": max(
                (float(point.get("Average", 0.0)) for point in points),
                default=0.0,
            ),
            "maximum_max": max(
                (float(point.get("Maximum", 0.0)) for point in points),
                default=0.0,
            ),
            "unit": next((point.get("Unit") for point in points if point.get("Unit")), None),
        }
    return result


def cleanup_evidence(
    instance_id: str,
    key_name: str,
    security_group_id: str,
    branch_id: str,
    branch_name: str,
    local_environment_path: Path,
) -> dict:
    instance = command(
        [
            "aws",
            "ec2",
            "describe-instances",
            "--region",
            "us-east-2",
            "--instance-ids",
            instance_id,
            "--query",
            "Reservations[0].Instances[0].State.Name",
            "--output",
            "text",
        ]
    )
    key = command(
        [
            "aws",
            "ec2",
            "describe-key-pairs",
            "--region",
            "us-east-2",
            "--key-names",
            key_name,
        ]
    )
    security_group = command(
        [
            "aws",
            "ec2",
            "describe-security-groups",
            "--region",
            "us-east-2",
            "--group-ids",
            security_group_id,
        ]
    )
    branches = command(
        [
            "neon",
            "branches",
            "list",
            "--project-id",
            "cold-silence-99209400",
            "--output",
            "json",
        ]
    )
    remaining = load_json(branches.stdout) if branches.returncode == 0 else []
    branch_deleted = not any(
        row.get("id") == branch_id or row.get("name") == branch_name
        for row in remaining
    )
    state = instance.stdout.strip() if instance.returncode == 0 else "unknown"
    cleanup = {
        "instance_id": instance_id,
        "instance_state": state,
        "key_pair_name": key_name,
        "key_pair_deleted": key.returncode != 0,
        "security_group_id": security_group_id,
        "security_group_deleted": security_group.returncode != 0,
        "neon_branch_id": branch_id,
        "neon_branch_name": branch_name,
        "neon_branch_deleted": branch_deleted,
        "remote_workspace_destroyed_with_instance": state == "terminated",
        "remote_environment_destroyed_with_instance": state == "terminated",
        "local_credentials_removed": not local_environment_path.exists(),
    }
    cleanup["complete"] = all(
        (
            state == "terminated",
            cleanup["key_pair_deleted"],
            cleanup["security_group_deleted"],
            cleanup["neon_branch_deleted"],
            cleanup["remote_workspace_destroyed_with_instance"],
            cleanup["remote_environment_destroyed_with_instance"],
            cleanup["local_credentials_removed"],
        )
    )
    return cleanup


def load_json(value: str) -> object:
    return json.loads(value)


def _attribution(levels: list[dict], errors: list[dict]) -> dict:
    first_hard_failure = next(
        (level for level in levels if level["hard_failure_count"] > 0), None
    )
    pressure_levels = [
        level
        for level in levels
        if (
            level["host_cpu_percent_max"] >= THRESHOLDS["host_cpu_percent"]
            or level["memory_available_ratio_min"]
            <= THRESHOLDS["memory_available_ratio"]
            or (
                level["checkout_p95_ms"]
                >= THRESHOLDS["pool_checkout_p95_ms"]
                and level["pool_checked_out_max"] >= level["pool_capacity_max"]
            )
            or level["database_connection_ratio_max"]
            >= THRESHOLDS["database_connection_ratio"]
        )
    ]
    if not pressure_levels and first_hard_failure is None:
        return {
            "status": "boundary_not_reproduced",
            "boundary_concurrency": None,
            "first_failure_concurrency": None,
            "supported_causes": [],
            "unresolved": True,
        }

    error_types = sorted({event["error_type"] for event in errors})
    supported = []
    if any(
        level["host_cpu_percent_max"] >= THRESHOLDS["host_cpu_percent"]
        for level in pressure_levels
    ):
        supported.append("runner_cpu_pressure")
    if any(
        level["memory_available_ratio_min"]
        <= THRESHOLDS["memory_available_ratio"]
        for level in pressure_levels
    ):
        supported.append("runner_memory_pressure")
    if any(
        level["checkout_p95_ms"] >= THRESHOLDS["pool_checkout_p95_ms"]
        and level["pool_checked_out_max"] >= level["pool_capacity_max"]
        for level in pressure_levels
    ) or "TimeoutError" in error_types:
        supported.append("connection_pool_pressure")
    if any(
        level["database_connection_ratio_max"]
        >= THRESHOLDS["database_connection_ratio"]
        for level in pressure_levels
    ) or any("TooManyConnections" in name for name in error_types):
        supported.append("database_connection_pressure")
    application_errors = [
        name
        for name in error_types
        if name not in {"TimeoutError", "OperationalError", "InterfaceError"}
    ]
    if application_errors:
        supported.append("application_error")
    return {
        "status": "supported" if supported else "observed_cause_not_isolated",
        "boundary_concurrency": (
            pressure_levels[0]["concurrency"]
            if pressure_levels
            else first_hard_failure["concurrency"]
        ),
        "first_failure_concurrency": (
            first_hard_failure["concurrency"] if first_hard_failure else None
        ),
        "supported_causes": supported,
        "error_types": error_types,
        "unresolved": not supported,
    }


def compare(args: argparse.Namespace) -> dict:
    load_result = load(LOAD_RESULT)
    branch_rows = load(args.branches_json)
    endpoint_payload = load(args.endpoints_json)
    consumption = load(args.consumption_json)
    branch = next(row for row in branch_rows if row["id"] == args.branch_id)
    endpoints = endpoint_payload.get("endpoints", endpoint_payload)
    endpoint = next(row for row in endpoints if row["branch_id"] == args.branch_id)
    levels = [summarize_level(load_result, level) for level in load_result["levels"]]
    cleanup = cleanup_evidence(
        args.instance_id,
        args.key_name,
        args.security_group_id,
        args.branch_id,
        args.branch_name,
        args.local_environment_path,
    )
    attribution = _attribution(levels, load_result["telemetry"]["error_events"])
    result = {
        "milestone": "m8-20",
        "status": (
            "supported_diagnosis"
            if attribution["status"] == "supported"
            else "measured_inconclusive"
        ),
        "production_behavior_changed": False,
        "production_infrastructure_changed": False,
        "inputs": {path: digest(ROOT / path) for path in SOURCE_PATHS},
        "environment": {
            "provider": "aws",
            "region": "us-east-2",
            "availability_zone": args.availability_zone,
            "instance_type": args.instance_type,
            "api_processes": load_result["environment"]["api_processes"],
            "worker_processes": load_result["environment"]["worker_processes"],
            "host_cpu_count": load_result["environment"]["host_cpu_count"],
            "seconds_per_level": load_result["environment"]["seconds_per_level"],
            "concurrency_levels": load_result["environment"]["concurrency_levels"],
            "neon": {
                "project_id": branch["project_id"],
                "branch_id": branch["id"],
                "branch_name": branch["name"],
                "parent_id": branch["parent_id"],
                "endpoint_id": endpoint["id"],
                "autoscaling_limit_min_cu": endpoint.get("autoscaling_limit_min_cu"),
                "autoscaling_limit_max_cu": endpoint.get("autoscaling_limit_max_cu"),
                "suspend_timeout_seconds": endpoint.get("suspend_timeout_seconds"),
                "usage_before_cleanup": {
                    key: branch.get(key)
                    for key in (
                        "active_time_seconds",
                        "compute_time_seconds",
                        "cpu_used_sec",
                        "data_transfer_bytes",
                        "logical_size",
                        "written_data_bytes",
                    )
                },
                "consumption": consumption,
            },
        },
        "dataset": load_result["dataset"],
        "core_flow": load_result["core_flow"],
        "thresholds": THRESHOLDS,
        "by_concurrency": levels,
        "error_events": load_result["telemetry"]["error_events"],
        "cloudwatch": _cloudwatch(args.cloudwatch_dir),
        "attribution": attribution,
        "findings": [
            {
                "cause": "Redis cache requirement",
                "status": "not_supported",
                "evidence": "capacity signals do not establish a safe cache invalidation benefit",
            },
            {
                "cause": "graph database requirement",
                "status": "not_supported",
                "evidence": "the measured workload contains no multi-hop relationship query",
            },
        ],
        "cleanup": cleanup,
        "limits": [
            "This is a bounded capacity attribution run, not the protected release profile.",
            "Neon branch consumption can be delayed and does not expose host CPU utilization.",
            "A supported signal selects the next experiment; it does not authorize a production change.",
            "The result does not change the production release decision.",
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
    parser.add_argument("--branch-id", required=True)
    parser.add_argument("--branch-name", required=True)
    parser.add_argument("--branches-json", type=Path, required=True)
    parser.add_argument("--endpoints-json", type=Path, required=True)
    parser.add_argument("--consumption-json", type=Path, required=True)
    parser.add_argument("--cloudwatch-dir", type=Path, required=True)
    parser.add_argument("--local-environment-path", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(compare(args), indent=2))


if __name__ == "__main__":
    main()

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "m8_capacity", ROOT / "evals/m8/compare_capacity.py"
)
COMPARE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(COMPARE)


def evidence() -> dict:
    return json.loads(COMPARE.OUTPUT.read_text())


def test_capacity_profile_is_source_bound_and_attributed() -> None:
    result = evidence()

    assert result["milestone"] == "m8-20"
    assert result["production_behavior_changed"] is False
    assert result["production_infrastructure_changed"] is False
    assert result["environment"]["region"] == "us-east-2"
    assert result["environment"]["concurrency_levels"] == [1, 5, 10, 25, 50]
    assert result["core_flow"]["status"] == "pass"
    assert result["core_flow"]["post_forget_status"] == 404
    assert result["dataset"]["canonical_memories"] == 1_000_000
    assert result["dataset"]["organizational_documents"] == 10_000
    assert all(
        COMPARE.digest(ROOT / path) == expected
        for path, expected in result["inputs"].items()
    )

    levels = result["by_concurrency"]
    assert [item["concurrency"] for item in levels] == [1, 5, 10, 25, 50]
    assert all(item["requests"] > 0 for item in levels)
    assert all(item["telemetry_samples"] >= 2 for item in levels)
    assert all(item["database_max_connections"] > 0 for item in levels)
    assert all(item["pool_capacity_max"] > 0 for item in levels)
    assert result["attribution"]["status"] in {
        "supported",
        "observed_cause_not_isolated",
        "boundary_not_reproduced",
    }
    assert result["attribution"]["unresolved"] is (
        not bool(result["attribution"]["supported_causes"])
    )
    assert result["environment"]["neon"]["autoscaling_limit_max_cu"] > 0
    assert "CPUUtilization" in result["cloudwatch"]
    findings = {item["cause"]: item for item in result["findings"]}
    assert findings["Redis cache requirement"]["status"] == "not_supported"
    assert findings["graph database requirement"]["status"] == "not_supported"


def test_capacity_run_proves_cleanup() -> None:
    cleanup = evidence()["cleanup"]

    assert cleanup["complete"] is True
    assert cleanup["instance_state"] == "terminated"
    assert cleanup["key_pair_deleted"] is True
    assert cleanup["security_group_deleted"] is True
    assert cleanup["neon_branch_deleted"] is True
    assert cleanup["remote_workspace_destroyed_with_instance"] is True
    assert cleanup["remote_environment_destroyed_with_instance"] is True
    assert cleanup["local_credentials_removed"] is True

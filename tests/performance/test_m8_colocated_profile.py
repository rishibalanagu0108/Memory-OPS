import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "m8_colocated", ROOT / "evals/m8/compare_colocated.py"
)
COMPARE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(COMPARE)


def evidence() -> dict:
    return json.loads(COMPARE.OUTPUT.read_text())


def test_colocated_profile_is_current_and_uses_the_same_workload() -> None:
    result = evidence()

    assert result["milestone"] == "m8-17"
    assert result["production_behavior_changed"] is False
    assert result["same_workload_sources"] is True
    assert result["environment"]["region"] == "us-east-2"
    assert result["environment"]["instance_type"] == "t3.small"
    assert result["environment"]["concurrency_levels"] == [1, 5, 10, 25, 50]
    assert result["core_flow"]["status"] == "pass"
    assert result["core_flow"]["post_forget_status"] == 404
    assert all(
        COMPARE.digest(ROOT / path) == expected
        for path, expected in result["inputs"].items()
    )


def test_colocation_comparison_is_complete_and_evidence_bound() -> None:
    result = evidence()

    assert [item["concurrency"] for item in result["by_concurrency"]] == [1, 5, 10, 25, 50]
    assert all(item["local"]["requests"] > 0 for item in result["by_concurrency"])
    assert all(item["colocated"]["requests"] > 0 for item in result["by_concurrency"])
    assert all(item["ratios"]["p95"] > 0 for item in result["by_concurrency"])
    assert all(
        item["colocated"]["hard_failure_count"] == 0
        for item in result["by_concurrency"][:2]
    )
    assert result["by_concurrency"][2]["colocated"]["hard_failure_count"] > 0
    findings = {item["cause"]: item for item in result["findings"]}
    assert findings["API-to-database topology cost"]["status"] == "supported"
    assert findings["same-region capacity boundary"]["status"] == (
        "observed_cause_not_isolated"
    )
    assert findings["Redis cache requirement"]["status"] == "not_supported"
    assert findings["graph database requirement"]["status"] == "not_supported"


def test_all_temporary_aws_resources_and_secrets_are_removed() -> None:
    cleanup = evidence()["cleanup"]

    assert cleanup["complete"] is True
    assert cleanup["instance_state"] == "terminated"
    assert cleanup["key_pair_deleted"] is True
    assert cleanup["security_group_deleted"] is True
    assert cleanup["remote_workspace_destroyed_with_instance"] is True
    assert cleanup["remote_environment_destroyed_with_instance"] is True

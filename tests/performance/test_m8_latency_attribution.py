import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "m8_latency_attribution", ROOT / "evals/m8/latency_attribution.py"
)
ATTRIBUTION = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ATTRIBUTION)


def evidence() -> dict:
    return json.loads(ATTRIBUTION.OUTPUT.read_text())


def test_latency_evidence_is_current_and_covers_the_approved_sweep() -> None:
    result = evidence()

    assert result["milestone"] == "m8-16"
    assert result["status"] == "supported_diagnosis"
    assert result["production_behavior_changed"] is False
    assert result["environment"]["concurrency_levels"] == [1, 5, 10, 25, 50]
    assert [level["concurrency"] for level in result["levels"]] == [1, 5, 10, 25, 50]
    assert all(level["requests"] > 0 for level in result["levels"])
    assert all(
        ATTRIBUTION.digest(ROOT / path) == expected
        for path, expected in result["inputs"].items()
    )


def test_core_memory_lifecycle_passes_and_forgetting_is_immediate() -> None:
    result = evidence()

    assert result["core_flow"]["status"] == "pass"
    assert result["core_flow"]["post_forget_status"] == 404
    assert set(result["core_flow"]["latency"]) == {
        "remember",
        "retrieve",
        "correct",
        "context",
        "forget",
        "post_forget_retrieve",
    }


def test_each_level_attributes_latency_without_inventing_infrastructure_need() -> None:
    result = evidence()

    for level in result["levels"]:
        assert not level["errors"]
        assert set(level["overall"]) == set(ATTRIBUTION.COMPONENTS)
        assert {
            "health",
            "operation_status",
            "memory_inspect",
            "context_build",
            "knowledge_search",
            "canonical_write",
        }.issubset(level["by_operation"])
        assert level["overall"]["client_wall_ms"]["p95"] >= level["overall"][
            "server_wall_ms"
        ]["p50"]

    findings = {item["cause"]: item for item in result["findings"]}
    assert findings["single-client dominant latency component"]["status"] == "supported"
    assert findings["Redis cache requirement"]["status"] == "not_supported"
    assert findings["graph database requirement"]["status"] == "not_supported"
    assert any("not a pure network measurement" in limit for limit in result["limits"])

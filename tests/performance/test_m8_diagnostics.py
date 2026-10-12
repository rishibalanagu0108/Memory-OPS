import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("m8_diagnose", ROOT / "evals/m8/diagnose.py")
DIAGNOSE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(DIAGNOSE)


def test_diagnostic_model_detects_the_remediated_runtime_shape() -> None:
    source = DIAGNOSE.source_diagnostics()

    assert source["knowledge_search_statement_count"] == 5
    assert source["candidate_hydration_is_n_plus_one"] is False
    assert source["worker_dispatch_is_serial"] is True
    assert source["load_concurrency"] == 50
    assert source["api_processes"] == 4


def test_published_failure_is_reproduced_and_recovery_rate_is_insufficient() -> None:
    result = DIAGNOSE.load_diagnostics(DIAGNOSE.load(DIAGNOSE.PUBLISHED), DIAGNOSE.load(DIAGNOSE.REPRODUCTION))

    assert result["failure_reproduced"] is True
    recovery = result["worker_recovery"]
    assert recovery["observed_events_per_second"] < recovery["required_events_per_second"]


def test_historical_diagnosis_and_current_remediation_are_distinct() -> None:
    diagnosis = json.loads((ROOT / "evals/m8/diagnostics.json").read_text())
    remediation = json.loads(
        (ROOT / "tests/performance/m8_09_result.json").read_text()
    )

    assert diagnosis["status"] == "supported_diagnosis"
    assert diagnosis["live"]["user_context"]["count"] == 83
    assert diagnosis["live"]["knowledge_search"]["count"] == 5
    assert diagnosis["source"]["worker_dispatch_is_serial"] is True
    assert diagnosis["inputs"]["src/memory_ops/context/__init__.py"] != (
        DIAGNOSE.digest(ROOT / "src/memory_ops/context/__init__.py")
    )
    assert remediation["status"] == "candidate_improvement"
    assert remediation["before"]["statement_count"] == 83
    assert remediation["after"]["statement_count"] == 7
    assert all(
        DIAGNOSE.digest(ROOT / path) == expected
        for path, expected in remediation["inputs"].items()
    )
    assert any(
        "does not claim production readiness" in limit
        for limit in remediation["limits"]
    )

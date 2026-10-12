import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("m8_diagnose", ROOT / "evals/m8/diagnose.py")
DIAGNOSE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(DIAGNOSE)


def test_diagnostic_model_matches_current_runtime_shape() -> None:
    source = DIAGNOSE.source_diagnostics()

    assert source["user_context_statement_ceiling"] == 83
    assert source["knowledge_search_statement_count"] == 5
    assert source["cross_domain_statement_ceiling"] == 88
    assert source["candidate_hydration_is_n_plus_one"] is True
    assert source["worker_dispatch_is_serial"] is True
    assert source["load_concurrency"] == 50
    assert source["api_processes"] == 4


def test_published_failure_is_reproduced_and_recovery_rate_is_insufficient() -> None:
    result = DIAGNOSE.load_diagnostics(DIAGNOSE.load(DIAGNOSE.PUBLISHED), DIAGNOSE.load(DIAGNOSE.REPRODUCTION))

    assert result["failure_reproduced"] is True
    recovery = result["worker_recovery"]
    assert recovery["observed_events_per_second"] < recovery["required_events_per_second"]


def test_diagnostics_are_source_bound_and_do_not_claim_a_fix() -> None:
    evidence = json.loads((ROOT / "evals/m8/diagnostics.json").read_text())

    assert evidence["status"] == "supported_diagnosis"
    assert evidence["live"]["dataset"] == {"canonical_memories": 1_000_000, "documents": 10_000}
    assert evidence["live"]["user_context"]["count"] == 83
    assert evidence["live"]["knowledge_search"]["count"] == 5
    assert evidence["source"]["worker_dispatch_is_serial"] is True
    assert all(DIAGNOSE.digest(ROOT / path) == expected for path, expected in evidence["inputs"].items())
    assert any("does not change production" in limit for limit in evidence["limits"])

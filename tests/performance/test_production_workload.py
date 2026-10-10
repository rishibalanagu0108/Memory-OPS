import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def test_declared_profile_and_operation_mix_are_complete() -> None:
    contract = load("evals/m8/contract.yaml")
    workload = load("evals/m8/workload.json")

    assert sum(workload["operation_mix_percent"].values()) == 100
    assert workload["profile"]["steady_state_qps"] >= contract["slos"]["synchronous_api"]["throughput_qps_min"]
    assert workload["profile"]["soak_minutes"] == workload["scenarios"][1]["expected"]["duration_minutes"]
    assert {case["category"] for case in workload["scenarios"]} == set(contract["promotion"]["required_suites"])


def test_fullscale_evidence_is_complete_and_failed_slos_block_release() -> None:
    contract = load("evals/m8/contract.yaml")
    workload = load("evals/m8/workload.json")
    measured = load("evals/m8/fullscale-result.json")
    result_path = ROOT / "evals/m8/result.json"
    assert measured["duration_seconds"] >= workload["profile"]["soak_minutes"] * 60
    assert measured["dataset"]["canonical_memory_count"] == workload["profile"]["canonical_memory_count"]
    assert measured["dataset"]["organizational_document_count"] == workload["profile"]["organizational_document_count"]
    assert measured["dataset"]["logical_source_bytes"] >= workload["profile"]["source_storage_gib"] * 1024**3
    assert measured["throughput_qps"] < contract["slos"]["synchronous_api"]["throughput_qps_min"]
    assert measured["latency_ms"]["p95"] > contract["slos"]["synchronous_api"]["p95_latency_ms_max"]
    assert measured["index_lag_seconds"]["p95"] > contract["slos"]["derived_index_freshness"]["p95_seconds_max"]
    assert measured["recovery"]["remaining_after_drain"] > 0
    assert measured["fault"]["acknowledged_write_loss_count"] == 0
    assert measured["cross_tenant_disclosure_count"] == 0
    assert measured["post_revocation_disclosure_count"] == 0
    assert measured["cost"]["launch_conservative_variable_usd_per_1000_operations"] <= contract["slos"]["cost"]["variable_usd_per_1000_operations_max"]
    assert measured["cost"]["launch_conservative_monthly_usd_at_2_cu"] <= contract["slos"]["cost"]["estimated_monthly_usd_max"]
    assert measured["dataset"]["object_bytes_materialized"] is False

    if result_path.exists():
        result = load("evals/m8/result.json")
        assert result["production_release"]["status"] == "blocked"
        assert result["claims"]["production_slo_met"] is False

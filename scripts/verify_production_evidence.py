"""Regenerate and validate the source-bound M8 production evidence."""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.m8.evaluate import RESULT, SUITES, evaluate, load, source_hash  # noqa: E402


def main() -> None:
    observed = evaluate()
    result = load(RESULT)

    assert result["status"] == "pass"
    assert result["source_hash"] == source_hash()
    assert set(result["suites"]) == set(SUITES)
    assert all(suite["status"] == "pass" for suite in result["suites"].values())
    assert all(suite["status"] == "pass" for suite in observed["suites"].values())
    assert result["evidence_class"] == "fullscale_database_and_api"
    assert result["fullscale_result"]["database_profile_exercised"] is True
    assert result["fullscale_result"]["duration_seconds"] >= 3600
    assert result["declared_profile_exercised"] is False
    assert result["production_release"]["status"] == "blocked"
    assert result["claims"]["production_slo_met"] is False
    assert result["claims"]["production_cost_slo_met"] is True
    assert result["claims"]["hard_safety_regression_observed"] is False
    print("M8 production evidence is complete; production qualification remains blocked")


if __name__ == "__main__":
    main()

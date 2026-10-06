"""Validate that the protected M2 holdout is complete and independent."""

import json
from pathlib import Path

from memory_ops.lifecycle import PURGE_TARGETS, PurgeStatus


ROOT = Path(__file__).resolve().parents[2]
HOLDOUT = json.loads((ROOT / "evals/m2/holdout.json").read_text())
GOLDEN = json.loads((ROOT / "evals/m2/golden.json").read_text())


def test_holdout_is_versioned_complete_and_separate_from_development() -> None:
    cases = HOLDOUT["cases"]
    assert HOLDOUT["split"] == "holdout"
    assert HOLDOUT["version"] == "1.0.0"
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["category"] for case in cases} == {
        "temporal",
        "correction",
        "conflict",
        "expiration",
        "deletion",
        "restore",
        "resurrection",
    }
    assert {case["id"] for case in cases}.isdisjoint(
        case["id"] for case in GOLDEN["cases"]
    )


def test_holdout_deletion_target_is_the_implemented_complete_set() -> None:
    purge_case = next(
        case for case in HOLDOUT["cases"] if case["id"] == "holdout-purge-all-targets"
    )
    assert purge_case["input"]["required_target_count"] == len(PURGE_TARGETS)
    assert PurgeStatus(tuple(sorted(PURGE_TARGETS))).deletion_completeness == 1.0

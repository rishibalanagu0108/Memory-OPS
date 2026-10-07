"""Blind review-sheet generation and validation."""

import csv

import pytest

from evals.m4.quality import expand_cases, load_manifest
from evals.m4.reviews import FIELDS, combine_reviews, write_sheet


def fill_sheet(path, decisions: dict[str, str]) -> None:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            row["decision"] = decisions[row["case_id"]]
            writer.writerow(row)


def test_blind_sheets_omit_expected_and_model_labels(tmp_path) -> None:
    sheet = tmp_path / "review.csv"
    write_sheet(sheet)

    with sheet.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 500
    assert tuple(rows[0]) == FIELDS
    assert all(row["decision"] == "" for row in rows)


def test_combine_requires_complete_independent_decisions(tmp_path) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    write_sheet(first)
    write_sheet(second)
    decisions = {case["id"]: case["expected_decision"] for case in expand_cases(load_manifest())}
    fill_sheet(first, decisions)
    fill_sheet(second, decisions)

    labels, disagreements = combine_reviews(first, "reviewer-a", second, "reviewer-b")

    assert len(labels["labels"]) == 1000
    assert disagreements == []
    with pytest.raises(ValueError, match="distinct reviewer"):
        combine_reviews(first, "same", second, "same")

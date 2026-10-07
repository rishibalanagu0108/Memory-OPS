"""Create and combine blind M4 human-review sheets."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from evals.m4.quality import expand_cases, load_manifest


FIELDS = ("case_id", "category", "utterance", "decision")
DECISIONS = {"extract", "abstain"}


def write_sheet(path: Path, *, force: bool = False) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite review work: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for case in expand_cases(load_manifest()):
            writer.writerow(
                {
                    "case_id": case["id"],
                    "category": case["category"],
                    "utterance": case["utterance"],
                    "decision": "",
                }
            )


def read_sheet(path: Path) -> list[dict]:
    expected = {case["id"]: case for case in expand_cases(load_manifest())}
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError(f"invalid columns in {path}")
        rows = list(reader)
    if len(rows) != len(expected) or {row["case_id"] for row in rows} != set(expected):
        raise ValueError(f"{path} must contain every review case exactly once")
    for row in rows:
        case = expected[row["case_id"]]
        if row["category"] != case["category"] or row["utterance"] != case["utterance"]:
            raise ValueError(f"case content changed in {path}: {row['case_id']}")
        row["decision"] = row["decision"].strip().lower()
        if row["decision"] not in DECISIONS:
            raise ValueError(f"missing or invalid decision in {path}: {row['case_id']}")
    return rows


def combine_reviews(
    sheet_a: Path,
    reviewer_a: str,
    sheet_b: Path,
    reviewer_b: str,
) -> tuple[dict, list[dict]]:
    if not reviewer_a.strip() or not reviewer_b.strip() or reviewer_a == reviewer_b:
        raise ValueError("two distinct reviewer names are required")
    rows_a = read_sheet(sheet_a)
    rows_b = {row["case_id"]: row for row in read_sheet(sheet_b)}
    labels = []
    disagreements = []
    for row_a in rows_a:
        row_b = rows_b[row_a["case_id"]]
        labels.extend(
            (
                {"case_id": row_a["case_id"], "reviewer_id": reviewer_a, "decision": row_a["decision"]},
                {"case_id": row_a["case_id"], "reviewer_id": reviewer_b, "decision": row_b["decision"]},
            )
        )
        if row_a["decision"] != row_b["decision"]:
            disagreements.append(
                {
                    "case_id": row_a["case_id"],
                    "category": row_a["category"],
                    "utterance": row_a["utterance"],
                    "reviewer_a_decision": row_a["decision"],
                    "reviewer_b_decision": row_b["decision"],
                    "final_decision": "",
                }
            )
    return {"labels": labels}, disagreements


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--output-dir", default=".reviews/m4")
    create.add_argument("--force", action="store_true")
    combine = commands.add_parser("combine")
    combine.add_argument("--sheet-a", default=".reviews/m4/rishik-kumar.csv")
    combine.add_argument("--reviewer-a", default="Rishik Kumar")
    combine.add_argument("--sheet-b", default=".reviews/m4/reviewer-b.csv")
    combine.add_argument("--reviewer-b", required=True)
    combine.add_argument("--output", default="evals/m4/reviewer_labels.json")
    combine.add_argument("--disagreements", default=".reviews/m4/disagreements.csv")
    args = parser.parse_args()

    if args.command == "create":
        output = Path(args.output_dir)
        write_sheet(output / "rishik-kumar.csv", force=args.force)
        write_sheet(output / "reviewer-b.csv", force=args.force)
        print(f"created {output / 'rishik-kumar.csv'}")
        print(f"created {output / 'reviewer-b.csv'}")
        return

    labels, disagreements = combine_reviews(
        Path(args.sheet_a), args.reviewer_a, Path(args.sheet_b), args.reviewer_b
    )
    Path(args.output).write_text(json.dumps(labels, indent=2) + "\n")
    disagreement_path = Path(args.disagreements)
    disagreement_path.parent.mkdir(parents=True, exist_ok=True)
    with disagreement_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "case_id", "category", "utterance", "reviewer_a_decision",
            "reviewer_b_decision", "final_decision",
        ))
        writer.writeheader()
        writer.writerows(disagreements)
    print(f"combined 1000 labels; {len(disagreements)} disagreements require adjudication")


if __name__ == "__main__":
    main()

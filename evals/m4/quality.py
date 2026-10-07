"""Credential-free M4 quality corpus expansion and scoring."""

from __future__ import annotations

import json
import hashlib
import math
import argparse
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATHS = (
    "src/memory_ops/extraction/__init__.py",
    "src/memory_ops/extraction/promotion.py",
    "evals/m4/quality.py",
    "evals/m4/quality_holdout.json",
)


def expand_cases(manifest: dict) -> list[dict]:
    cases = []
    for category, groups in sorted(manifest["categories"].items()):
        for expected, group in (("extract", groups["positive"]), ("abstain", groups["negative"])):
            for template_index, template in enumerate(group["templates"], start=1):
                for value_index, value in enumerate(group["values"], start=1):
                    cases.append(
                        {
                            "id": f"m4-quality-{category}-{expected}-{template_index:02d}-{value_index:02d}",
                            "category": category,
                            "utterance": template.format(value=value),
                            "expected_decision": expected,
                        }
                    )
    case_ids = {case["id"] for case in cases}
    if len(case_ids) != len(cases):
        raise ValueError("quality case IDs must be unique")
    return cases


def _kappa(left: list[str], right: list[str]) -> float:
    observed = sum(a == b for a, b in zip(left, right, strict=True)) / len(left)
    labels = {"extract", "abstain"}
    expected = sum(
        (left.count(label) / len(left)) * (right.count(label) / len(right))
        for label in labels
    )
    return 1.0 if expected == 1.0 and observed == 1.0 else (observed - expected) / (1 - expected)


def _calibration(probabilities: list[float], outcomes: list[int], bins: int) -> tuple[float, float]:
    brier = sum((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes, strict=True)) / len(outcomes)
    ece = 0.0
    for bin_index in range(bins):
        lower = bin_index / bins
        upper = (bin_index + 1) / bins
        members = [
            index
            for index, probability in enumerate(probabilities)
            if lower <= probability < upper or (bin_index == bins - 1 and probability == 1.0)
        ]
        if members:
            confidence = sum(probabilities[index] for index in members) / len(members)
            accuracy = sum(outcomes[index] for index in members) / len(members)
            ece += len(members) / len(outcomes) * abs(confidence - accuracy)
    return round(ece, 6), round(brier, 6)


def score_predictions(
    manifest: dict,
    predictions: dict,
    expected_decisions: dict[str, str] | None = None,
) -> dict:
    cases = expand_cases(manifest)
    case_by_id = {case["id"]: case for case in cases}
    prediction_by_id = {item["case_id"]: item for item in predictions["predictions"]}
    if set(prediction_by_id) != set(case_by_id) or len(prediction_by_id) != len(predictions["predictions"]):
        raise ValueError("candidate predictions must cover every quality case exactly once")
    category_results = {}
    for category in manifest["categories"]:
        category_cases = [case for case in cases if case["category"] == category]
        expected = [
            (expected_decisions or {}).get(case["id"], case["expected_decision"])
            == "extract"
            for case in category_cases
        ]
        predicted = []
        probabilities = []
        for case in category_cases:
            prediction = prediction_by_id[case["id"]]
            probability = prediction.get("extract_probability")
            if not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("extract_probability must be finite and between zero and one")
            predicted.append(
                prediction.get("decision") == "extract"
                and prediction.get("semantic_type") == category
            )
            probabilities.append(float(probability))

        true_positive = sum(actual and guess for actual, guess in zip(expected, predicted, strict=True))
        false_positive = sum(not actual and guess for actual, guess in zip(expected, predicted, strict=True))
        false_negative = sum(actual and not guess for actual, guess in zip(expected, predicted, strict=True))
        precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
        recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
        ece, brier = _calibration(probabilities, [int(value) for value in expected], manifest["calibration_bins"])
        category_results[category] = {
            "case_count": len(category_cases),
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "expected_calibration_error": ece,
            "brier_score": brier,
        }
    return category_results


def score_quality(manifest: dict, predictions: dict, reviews: dict) -> dict:
    cases = expand_cases(manifest)
    case_by_id = {case["id"]: case for case in cases}
    labels_by_case: dict[str, list[dict]] = defaultdict(list)
    for label in reviews["labels"]:
        labels_by_case[label["case_id"]].append(label)
    if set(labels_by_case) != set(case_by_id):
        raise ValueError("review labels must cover every quality case")
    if any(
        len(labels) != 2
        or len({label["reviewer_id"] for label in labels}) != 2
        or any(label["decision"] not in {"extract", "abstain"} for label in labels)
        for labels in labels_by_case.values()
    ):
        raise ValueError("every case requires two distinct valid reviewer labels")

    disagreements = {
        case_id
        for case_id, labels in labels_by_case.items()
        if len({label["decision"] for label in labels}) != 1
    }
    if disagreements:
        raise ValueError("review disagreements must be adjudicated before scoring")
    reviewer_consensus = {
        case_id: labels[0]["decision"] for case_id, labels in labels_by_case.items()
    }
    category_results = score_predictions(manifest, predictions, reviewer_consensus)
    for category in manifest["categories"]:
        category_cases = [case for case in cases if case["category"] == category]
        reviewer_left = []
        reviewer_right = []
        for case in category_cases:
            first, second = sorted(labels_by_case[case["id"]], key=lambda item: item["reviewer_id"])
            reviewer_left.append(first["decision"])
            reviewer_right.append(second["decision"])
        raw_agreement = sum(a == b for a, b in zip(reviewer_left, reviewer_right, strict=True)) / len(reviewer_left)
        category_results[category].update(
            reviewer_kappa=round(_kappa(reviewer_left, reviewer_right), 6),
            raw_agreement_rate=round(raw_agreement, 6),
        )
    return category_results


def load_manifest() -> dict:
    return json.loads((ROOT / "evals/m4/quality_holdout.json").read_text())


def _fingerprint(paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative_path in paths:
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update((ROOT / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def evaluate_predictions(contract: dict) -> dict:
    prediction_path = contract["holdout"]["candidate_predictions"]
    predictions = json.loads((ROOT / prediction_path).read_text())
    return {
        "model": predictions.get("model"),
        "category_results": score_predictions(load_manifest(), predictions),
        "source_fingerprint": _fingerprint((*SOURCE_PATHS, prediction_path)),
    }


def evaluate_files(contract: dict) -> dict:
    holdout = contract["holdout"]
    predictions = json.loads((ROOT / holdout["candidate_predictions"]).read_text())
    reviews = json.loads((ROOT / holdout["reviewer_labels"]).read_text())
    return {
        "model": predictions.get("model"),
        "category_results": score_quality(load_manifest(), predictions, reviews),
        "source_fingerprint": _fingerprint(
            (*SOURCE_PATHS, holdout["candidate_predictions"], holdout["reviewer_labels"])
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-packet", action="store_true")
    args = parser.parse_args()
    cases = expand_cases(load_manifest())
    if args.review_packet:
        cases = [
            {"case_id": case["id"], "category": case["category"], "utterance": case["utterance"]}
            for case in cases
        ]
    print(json.dumps({"cases": cases}, indent=2))

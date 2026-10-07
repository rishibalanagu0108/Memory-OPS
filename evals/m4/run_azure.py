"""Run the protected M4 corpus through an Azure OpenAI deployment."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from evals.m4.quality import expand_cases, load_manifest


ROOT = Path(__file__).resolve().parents[2]
CATEGORIES = ["fact", "preference", "goal", "constraint", "episode"]


def load_env(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text().splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    required = {"AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"}
    missing = sorted(key for key in required if not values.get(key))
    if missing:
        raise ValueError(f"missing environment values: {', '.join(missing)}")
    return values


def response_text(payload: dict) -> str:
    for output in payload.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "output_text":
                return content["text"]
    raise ValueError("Azure response contained no output text")


def classify_batch(values: dict[str, str], cases: list[dict]) -> tuple[list[dict], str]:
    schema = {
        "type": "object",
        "properties": {
            "predictions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string"},
                        "decision": {"type": "string", "enum": ["extract", "abstain"]},
                        "semantic_type": {"type": ["string", "null"], "enum": [*CATEGORIES, None]},
                        "extract_probability": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["case_id", "decision", "semantic_type", "extract_probability"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["predictions"],
        "additionalProperties": False,
    }
    body = {
        "model": values["AZURE_OPENAI_DEPLOYMENT"],
        "instructions": (
            "Classify each user utterance for durable personal-memory extraction. Extract only an explicit, "
            "user-specific fact, preference, current goal, hard constraint, or completed autobiographical episode. "
            "Abstain on questions, hypotheticals, third-party claims, completed goals, dreams, and explicit requests "
            "not to record. Return every case exactly once. semantic_type must be null when abstaining. "
            "extract_probability is the probability that extraction is correct, not stylistic confidence."
        ),
        "input": json.dumps([{"case_id": case["id"], "utterance": case["utterance"]} for case in cases]),
        "text": {"format": {"type": "json_schema", "name": "memory_extraction", "strict": True, "schema": schema}},
    }
    request = Request(
        values["AZURE_OPENAI_BASE_URL"].rstrip("/") + "/responses",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {values['AZURE_OPENAI_API_KEY']}",
            "api-key": values["AZURE_OPENAI_API_KEY"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    for attempt in range(4):
        try:
            with urlopen(request, timeout=120) as response:
                payload = json.load(response)
                parsed = json.loads(response_text(payload))["predictions"]
                if {item["case_id"] for item in parsed} != {case["id"] for case in cases}:
                    raise ValueError("Azure response did not cover the requested batch")
                return parsed, payload.get("model", values["AZURE_OPENAI_DEPLOYMENT"])
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                try:
                    message = json.load(error).get("error", {}).get("message", "request failed")
                except Exception:
                    message = "request failed"
                raise RuntimeError(f"Azure request failed ({error.code}): {message}") from error
            time.sleep(min(2**attempt, 8))
    raise AssertionError("unreachable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.local")
    parser.add_argument("--output", default="evals/m4/candidate_predictions.json")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")

    values = load_env(ROOT / args.env_file)
    cases = expand_cases(load_manifest())
    if args.limit is not None:
        cases = cases[: args.limit]
    predictions = []
    model_version = values["AZURE_OPENAI_DEPLOYMENT"]
    for start in range(0, len(cases), args.batch_size):
        batch, model_version = classify_batch(values, cases[start : start + args.batch_size])
        predictions.extend(batch)
        print(f"evaluated {len(predictions)}/{len(cases)}", flush=True)

    output = {
        "model": {
            "provider": "azure-openai",
            "name": values["AZURE_OPENAI_DEPLOYMENT"],
            "version": model_version,
        },
        "predictions": predictions,
    }
    (ROOT / args.output).write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    main()

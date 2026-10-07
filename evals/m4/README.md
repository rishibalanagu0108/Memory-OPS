# M4 quality evaluation

M4-06 measures the shadow extractor against a 500-case protected holdout and two independent
human reviews. It does not enable automatic canonical persistence.

Materialize the 500 labeled cases without any API credentials:

```bash
uv run python -m evals.m4.quality > /tmp/m4-quality-cases.json
```

Create two blind CSV sheets that omit expected labels and model predictions:

```bash
uv run python -m evals.m4.reviews create
```

Reviewer A edits `.reviews/m4/rishik-kumar.csv`; Reviewer B edits
`.reviews/m4/reviewer-b.csv`. Change only the `decision` column to `extract` or `abstain`.
Do not compare sheets until both reviewers finish.

Then combine them with Reviewer B's real name:

```bash
uv run python -m evals.m4.reviews combine \
  --reviewer-b "REVIEWER B NAME" \
  --sheet-b ".reviews/m4/REVIEWER-B-FILE.csv"
```

This creates `evals/m4/reviewer_labels.json` and a local
`.reviews/m4/disagreements.csv`. Every disagreement must receive a final adjudicated decision
before M4 can complete.

The configured extractor must produce `candidate_predictions.json` with model identity and one
prediction per case:

```json
{
  "model": {"provider": "provider", "name": "model", "version": "version"},
  "predictions": [
    {
      "case_id": "m4-quality-fact-extract-01-01",
      "decision": "extract",
      "semantic_type": "fact",
      "extract_probability": 0.99
    }
  ]
}
```

For Azure OpenAI, run a small smoke batch before the full evaluation:

```bash
uv run python -m evals.m4.run_azure --limit 5 --output /tmp/m4-smoke.json
uv run python -m evals.m4.run_azure
```

Two blinded reviewers must independently produce `reviewer_labels.json`:

```json
{
  "labels": [
    {"case_id": "m4-quality-fact-extract-01-01", "reviewer_id": "reviewer-a", "decision": "extract"},
    {"case_id": "m4-quality-fact-extract-01-01", "reviewer_id": "reviewer-b", "decision": "extract"}
  ]
}
```

The evaluator rejects incomplete case coverage, duplicate evidence, reviewer disagreements,
invalid probabilities, and anything other than two distinct labels per case. The Azure candidate
run is recorded in `candidate_predictions.json`; the approved, source-bound outcome is in
`quality_result.json`.

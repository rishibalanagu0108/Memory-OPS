# M6 organizational-knowledge evaluation

M6 uses a credential-free protected holdout distinct from the development corpus. It covers
structure-aware Markdown parsing, authorized retrieval, current-version filtering, conflicting
sources, untrusted instructions, stale projections, exact citations, and cross-tenant denial.

Run the source-bound evaluation:

```bash
uv run python -m evals.m6.evaluate
```

Run the release gate:

```bash
uv run python scripts/verify_milestone.py m6
```

The deterministic evaluator makes no external model calls. Its evidence-replay benchmark measures
local evaluation overhead; the release gate separately runs the PostgreSQL-backed knowledge tests
that exercise ingestion, parsing, retrieval, citations, ACL revisions, and lifecycle changes.

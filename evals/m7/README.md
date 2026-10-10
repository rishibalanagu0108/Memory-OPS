# M7 cross-domain context evaluation

M7 uses a credential-free protected holdout separate from its development corpus. Ten paired cases
cover domain authority, explicit conflicts, unavailable domains, token budgets, and deterministic
end-task outcomes. The baseline flattens authorized items; the candidate executes the real M7 router
and assembler while preserving labels, provenance, warnings, conflicts, and partial status.

Run the source-bound evaluation:

```bash
uv run --env-file .env.test python -m evals.m7.evaluate
```

Run the release gate:

```bash
uv run --env-file .env.test python scripts/verify_milestone.py m7
```

The evaluator makes no external model or judge calls. Deterministic quality and safety measurements
are bound to the contract, holdout, evaluator, routing code, API surface, and OpenAPI document.

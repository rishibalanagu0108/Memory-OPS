# M4 governed extraction architecture

Status: **implemented and verified as fail-closed; automatic extraction remains disabled**.

![M4 extraction architecture](m4-extraction.svg)

The editable source is [`m4-extraction.mmd`](m4-extraction.mmd).

## Shadow and review boundary

Conversation input enters a non-persisting shadow pipeline. Authentication, tenant/workspace
authorization, prohibited-secret checks, explicit correction/forget/remember precedence, and
machine policy run before a candidate can reach human review. A reviewer decision is audited,
but the current M4 path still performs zero canonical writes.

Promotion is independent for fact, preference, goal, constraint, and episode. Each category
requires a source-bound protected holdout, at least 100 labeled cases, precision and recall
floors, calibration, reviewer agreement, regression evidence, and explicit approval. Monitoring
can pause an automatic category and rollback returns it to shadow mode.

## Holdout decision

The M4 readiness holdout and 25 extraction tests passed with zero automatic canonical writes,
secret acceptances, policy overrides, or explicit-operation overrides. No production extractor
is configured, however, so quality, calibration, and reviewer agreement were not measured.
All five categories therefore remain in shadow mode. “Pass” means the gate failed closed; it is
not a claim that automatic extraction is ready.

Evidence: [`../../../evals/m4/result.json`](../../../evals/m4/result.json).

# M5 agent-learning evaluation

M5 compares the same task, Azure OpenAI deployment, tool versions, environment, and logical seed
twice: a baseline without a lesson and a candidate arm with one scoped lesson. Five repetitions of
12 protected cases produce 60 paired observations. Deterministic action assertions and the
human-labelled applicability in the dataset are authoritative; there is no external model judge.

The first pilot used semantically obvious action names, allowing the baseline to infer every
answer, and its safety flag did not distinguish detecting an override request from executing one.
That pilot was retired before a promotion decision. Holdout `1.1.0` uses previously unseen opaque
action codes for applicable lessons and an explicit `policy_override_executed` field.

Run the paired Azure measurement with the existing local credential:

```bash
uv run python -m evals.m5.run_azure
```

Recompute the source-bound result:

```bash
uv run python -m evals.m5.evaluate > evals/m5/result.json
```

The final run measured candidate success `1.0` versus baseline `0.666667`; the paired quality-delta
lower confidence bound was `0.213045`. Applicable lesson use was `1.0`, all scope and hard-safety
counts were zero, p95 latency was `900.145 ms`, latency ratio was `0.893982`, estimated cost was
`$3.960833` per 1,000 tasks, and cost ratio was `0.968379`. Cost uses measured token counts and the
contract's conservative rate card, not an Azure invoice.

The result makes the evaluated candidate eligible for explicit approval and canary use. It does
not automatically promote a lesson.

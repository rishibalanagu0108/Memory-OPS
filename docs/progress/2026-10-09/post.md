# Memory-ops: lessons earn canary access; they do not grant it themselves

M5 closes the agent-learning loop with measured evidence and explicit control boundaries.

The protected holdout ran 60 paired observations through the same Azure OpenAI deployment: each
task used the same model, tool versions, environment, and logical seed, while only lesson
availability changed. Candidate success was 1.0 versus a 0.667 baseline, and the lower confidence
bound on the paired quality improvement was 0.213. Applicable lesson use was perfect, all scope
and hard-safety violation counts were zero, and latency and estimated token cost stayed within the
predeclared absolute and relative limits.

The evaluation itself also caught a measurement flaw. A pilot used action names whose semantics
leaked the answer to the baseline and an ambiguous safety flag. That pilot was retired before any
promotion decision; the final versioned holdout used unseen opaque action codes and measured
executed policy overrides explicitly.

Passing does not turn a lesson on. The result only makes an immutable, source-bound lesson version
eligible for human approval and deterministic canary use. Monitoring can activate or pause it,
and rollback always makes it unselectable.

![M5 evidence flow](diagram.svg)

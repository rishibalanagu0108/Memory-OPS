# M5 agent-learning evaluation

M5 compares the same task, model, tool versions, environment, and seed twice: once without a
lesson and once with a scoped candidate lesson. This isolates the lesson's effect instead of
crediting a different model or easier task.

The development suite contains deterministic success assertions and human-labelled
applicability. External model graders are not authoritative. Runs may record observable inputs,
actions, outputs, timing, token use, and cost, but must not store credentials, private
chain-of-thought, or arbitrary sensitive tool output.

The protected holdout remains unpublished and pending until `M5-05`. A candidate lesson cannot
change behavior unless its task-quality delta has a positive lower confidence bound, every hard
safety gate passes, its scope matches, cost and latency stay within budget, approval is recorded,
and rollback is available.

The existing Azure OpenAI configuration can drive both paired arms. No additional provider
credential is required; use the same deployment for baseline and candidate runs.

# M8 operations and release guide

Status: **production release blocked**. This guide describes the implemented operating boundary and
the evidence required to change that decision; it is not a launch authorization.

## Normal operation

Run the modular API and worker as separately supervised processes in one approved region. Use pooled
TLS PostgreSQL connections for application traffic and a direct TLS connection for migrations and
administrative recovery. Keep canonical versions, grants, lifecycle state, idempotency receipts, and
the transactional outbox in PostgreSQL. Treat indexes as rebuildable derivatives and source objects
as policy-controlled data, never as authorization.

Identity or policy failure must fail closed. Optional enrichment failure must be explicit and may
degrade retrieval, but must not fabricate completeness. Logs, traces, metrics, ordinary audit
events, incident records, and evaluation summaries must remain content-free.

## Release decision

The current decision is NO-GO. Do not route production traffic while any blocker in
[`../../evals/m8/result.json`](../../evals/m8/result.json) remains open. A release candidate needs a
new protected run, bound to its source hash, that materializes the declared object profile, measures
API and restarted-worker memory reliably, and passes every latency, throughput, freshness, recovery,
cost, and hard-safety threshold. Human approval remains mandatory.

No failed gate may be waived by a passing average, cost estimate, or safety suite. Do not add Redis,
Kafka, Kubernetes, a dedicated vector/graph system, or a service split unless the contract's
component-extraction criteria are met on three consecutive representative runs and rollback is
proven.

## Observe and respond

Alert on API error rate and latency, outbox age and depth, index lag, stalled deletion, policy denial
changes, worker restarts, and database saturation. Telemetry identifies tenant/resource IDs only in
their approved opaque form and never records raw memory or document content.

For an incident, follow [incident response](incident-response.md): contain traffic, preserve
content-free evidence, fail policy closed, and recover offline. For data recovery, follow the
[deletion-aware recovery runbook](recovery-runbook.md); no restored environment may serve before the
external deletion ledger is replayed and resurrection checks pass.

## Change and rollback

Apply migrations through the direct connection, test upgrade and downgrade on an isolated expiring
branch, and verify data preservation before production scheduling. The M8 live drill proved the
current migration path with zero data loss; it does not approve future migrations automatically.
API clients must preserve canonical response shapes and tolerate additive fields according to the
published compatibility contract.

After every candidate, publish the immutable result, final security review, synchronized Mermaid
source and SVG, and an evidence-accurate learning artifact. If a run fails, keep the result and its
blockers visible; never relabel execution success as production readiness.

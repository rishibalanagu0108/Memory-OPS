# M8 production-blocker diagnosis

Status: **diagnosis supported; production behavior unchanged**.

An isolated Neon branch was seeded to the declared database shape of 1,000,000 canonical
memories and 10,000 organizational documents. A 299-second reproduction sustained 1.10 QPS,
recorded 47.8-second P95 latency, and observed 94.9-second P95 index lag. This reproduces the
published throughput, latency, and freshness failures without changing the production runtime.

## Supported causes

The dominant read-path problem is transaction and query amplification, not the full-text query.
One user-context build returned 20 items by issuing 83 SQL statements:

- one keyword-retrieval transaction: role setup, tenant context, and the search query;
- 20 separate canonical hydration transactions; and
- within every hydration transaction, role setup, tenant context, a version read, and an
  evidence read.

The isolated call took 51.45 seconds. Its full-text query used 298 ms, while repeated
`SET LOCAL ROLE` calls consumed 13.50 seconds, tenant-context calls consumed 6.70 seconds,
and canonical hydration plus evidence reads consumed 12.67 seconds. The remaining wall time
was client, pool, transaction, and network overhead across those repeated round trips. The
knowledge-domain search added five statements and 2.40 seconds. With both domains enabled,
the source-level ceiling is 88 statements for one context request.

The worker recovery failure has a separate supported cause: one worker claims a batch and
dispatches every event serially. The published recovery run drained only 43 of 257 events in
302.7 seconds (0.142 events/s), while clearing the same backlog inside five minutes requires
0.857 events/s. The reproduction still had 17 pending and two processing events when the
load process stopped.

Connection contention remains a measured contributor but is not isolated as an independent
root cause. Fifty concurrent load requests share four process-local SQLAlchemy pools, and each
context request repeatedly acquires tenant-scoped transactions. No blocking lock or SQL statement
running longer than 30 seconds was observed during the reproduction.

## Corrective order

1. Batch candidate hydration and evidence loading into one tenant transaction while preserving
   canonical-current, authorization, revocation, and policy checks.
2. Re-run the same operation-level profile before changing pool or compute sizing.
3. Make worker recovery parallel or set-based within explicit safety bounds, then verify expired
   leases, retries, idempotency, and deletion behavior.
4. Address the independent object-materialization and multi-process RSS evidence gaps in separate
   tasks before repeating the complete protected production profile.

The machine-readable evidence is in `evals/m8/diagnostics.json`. The original full-scale result
remains authoritative for the NO-GO decision.

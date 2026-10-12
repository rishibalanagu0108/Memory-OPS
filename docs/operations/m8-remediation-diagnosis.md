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

## M8-10 post-batch measurement

The five-minute representative rerun after batching canonical hydration completed 2,109 measured
requests at 6.59 QPS with 9.90-second overall P95 and 9.67-second context-build P95. Against the
M8-08 reproduction, throughput improved 5.98 times, overall P95 fell 79.3%, and context-build P95
fell 80.6%. The change is material, but it still misses the 25 QPS and 750 ms production objectives.

The run preserved a zero error rate, zero cross-tenant disclosures, zero post-revocation
disclosures, and zero acknowledged-write loss. It produced no completed operation-status samples,
so index-lag P95 is unmeasured rather than zero; the branch retained 292 pending and 52 processing
outbox events after the run. The source-bound comparison is in
`evals/m8/post-batch-profile.json`. Production remains blocked.

## M8-11 knowledge-search measurement

Knowledge search now stores and indexes its full-text vector and obtains authorized results,
incomplete-index status, and stale-source status in one application query. The representative
mixed-workload rerun reduced knowledge-search P95 from 6.68 seconds to 5.45 seconds, an 18.4%
improvement.

The wider profile did not improve: throughput changed from 6.59 to 6.30 QPS and overall P95
increased from 9.90 to 12.72 seconds as write, memory-search, and context-build latencies varied.
The result therefore supports only the targeted knowledge-search improvement, not an overall
capacity improvement. Safety remained at zero errors, cross-tenant disclosures,
post-revocation disclosures, and acknowledged-write loss. Index lag was again unmeasured because
no operation-status poll observed a completed measured write. The source-bound evidence is in
`evals/m8/knowledge-search-profile.json`; production remains blocked.

## M8-12 transaction-reuse measurement

Successful keyword or vector retrieval and canonical hydration now share one tenant transaction.
The 20-item user-context path fell from seven SQL statements to five while preserving an isolated
transaction for vector fallback after keyword failure.

Against the preceding representative run, throughput increased from 6.30 to 9.36 QPS, overall
P95 fell from 12.72 to 6.42 seconds, and context-build P95 fell from 14.96 to 5.72 seconds. The run
again recorded zero errors, cross-tenant disclosures, post-revocation disclosures, and
acknowledged-write loss. Pool sizes were not changed because this measurement supports round-trip
reduction and does not isolate pool wait as the next cause. Index lag remains unmeasured. The
source-bound evidence is in `evals/m8/transaction-reuse-profile.json`; production remains blocked.

## M8-13 worker-recovery measurement

The existing worker now dispatches each claimed batch with bounded in-process concurrency. The
default is eight threads, configurable from one to 32, and shutdown still waits for all events in
the claimed batch. Each event retains independent completion or retry handling.

On the isolated accumulated backlog, one worker drained 400 events in 357.3 seconds including
graceful batch completion: 1.119 events per second against the 0.857 events per second required to
clear the published 257-event outage backlog within five minutes. The final snapshot contained
zero processing leases and zero retries, with zero acknowledged-write loss. The source-bound
evidence is in `evals/m8/worker-recovery-profile.json`. The complete protected release profile and
remaining evidence gaps must still pass before production is unblocked.

## M8-14 integrated representative profile

With all approved remediations enabled, the representative mixed workload completed 2,750
measured requests at 8.71 QPS with 7.09-second overall P95 and 6.00-second context-build P95.
Against the M8-08 reproduction, throughput improved 7.90 times, overall P95 fell 85.2%, and
context-build P95 fell 87.9%. The run preserved zero errors, cross-tenant disclosures,
post-revocation disclosures, and acknowledged-write loss.

The result remains below the 25 QPS and 750 ms objectives. Index lag is unmeasured because no
operation-status poll observed a completed measured write. The protected hour-long profile,
object-byte materialization, and reliable multi-process RSS evidence also remain outstanding.
The source-bound comparison is in `evals/m8/integrated-profile.json`; production remains blocked.

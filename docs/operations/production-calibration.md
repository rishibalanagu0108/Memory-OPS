# Production calibration

Status: **declared for M8 implementation; not yet production-verified**.

## Workload boundary

V1 targets a small-to-medium, single-region deployment: one modular API, one independently runnable
worker, and managed PostgreSQL. The representative synthetic profile contains 100 tenants, 1,000
active principals, 1,000,000 canonical memories, 10,000 organizational documents, and 100 GiB of
source objects. Traffic is calibrated at 25 sustained requests per second, 50 peak, and a 100-RPS
60-second burst with 50 concurrent clients. The protected M8 run must use the operation mix and
scenarios in [`../../evals/m8/workload.json`](../../evals/m8/workload.json).

## Service objectives

| Surface | P50 | P95 | P99 | Minimum throughput |
|---|---:|---:|---:|---:|
| Synchronous API | 250 ms | 750 ms | 1,500 ms | 25 QPS |
| Async-work acceptance | 300 ms | 1,000 ms | 2,000 ms | 5 QPS |

Both surfaces allow at most a 1% non-policy error rate. Derived indexing must reach P95 within 30
seconds and P99 within 120 seconds. Variable service cost may not exceed USD 1 per 1,000 operations;
the calibrated workload may not exceed an estimated USD 300 monthly. Estimates must name their rate
card and may not be presented as invoices.

Regional restore targets RPO at most five minutes and RTO at most 60 minutes, with a 100% drill
success rate and zero deleted-content resurrection. Stalled durable work must recover within five
minutes with zero acknowledged-write loss. Revocation is synchronous: protected retrieval must deny
within one second and disclose nothing afterward.

## Provider and format constraints

- Managed PostgreSQL must use TLS, pooled application connections, a direct migration connection,
  and point-in-time recovery capable of the declared RPO.
- S3-compatible object storage stays in the same or an approved region, encrypts at rest, and deletes
  every version when policy requires purge.
- Human identity uses OIDC; agents use scoped credentials. Identity or policy failure fails closed.
- Model providers remain optional, sensitivity-controlled, absent from the critical synchronous path,
  provider-neutral at the public API, and excluded from raw-content telemetry.
- Sensitive content cannot cross an unapproved region or enter logs, traces, metrics, or ordinary
  audit events.

Supported uploads are UTF-8 plain text, Markdown, and JSON, plus PDF and DOCX, capped at 10 MB. A
format is not supported merely because object storage accepts its bytes; parsing, citations, and the
compatibility suite must pass first.

## Component-extraction gates

The modular baseline remains the production default. Redis, Kafka, Kubernetes, external queues,
dedicated search/vector or graph stores, and service splits are not preventive architecture.

Extraction requires three consecutive representative baseline failures or a binding residency or
regulatory requirement. On the same protected workload, the alternative must remove the failure and
either improve P95 by at least 30% or double throughput, without any hard-safety regression, with no
more than a 25% cost increase, and with a tested rollback. Search quality/latency can trigger a
dedicated search system; worker backlog recovery can trigger an external queue; measured replay plus
multiple consumers can trigger an event stream; demonstrable hot-read pressure can trigger a cache;
dominant multi-hop workloads can trigger a graph store; and measured independent scaling or failure
domains can trigger a service split. Until that evidence exists, the component stays in the modular
API, worker, and PostgreSQL baseline.

The binding machine-readable contract is
[`../../evals/m8/contract.yaml`](../../evals/m8/contract.yaml). M8-04 supplies protected load and
fault evidence; later M8 tasks supply restore, compatibility, migration, and final security evidence.

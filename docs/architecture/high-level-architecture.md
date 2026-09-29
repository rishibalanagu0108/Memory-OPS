# Memory-ops high-level architecture

Status: **planned V1 design — not yet implemented or evaluated**.

![Memory-ops high-level system architecture](high-level-architecture.svg)

The editable source is [`high-level-architecture.mmd`](high-level-architecture.mmd).

## How to read the diagram

- Follow steps **1–8** from left to right for the primary request-to-response journey.
- **Solid dark** arrows are synchronous operations.
- **Dashed purple** arrows are asynchronous, retryable work.
- **Red** is a denied path; **grey dotted** lines carry metadata only.
- Operations inside each box define that component’s high-level responsibility, not implementation detail.

## End-to-end journey

1. A client submits an explicit memory or context operation through an SDK or HTTPS.
2. The API validates the contract, applies versioning and idempotency, and routes the operation.
3. The trust boundary authenticates the principal, derives tenant/workspace scope, enforces grants, and applies machine policy.
4. The responsible domain writes or reads authoritative canonical state.
5. A successful write commits its outbox event atomically; workers later build or purge derivatives.
6. A context request searches each authorized domain independently.
7. Candidate IDs from derived indexes are revalidated and hydrated from current canonical state.
8. The response preserves domain labels and reports citations, conflicts, warnings, abstention, or degraded status honestly.

## System boundaries

| Component | Owns | Must not do |
|---|---|---|
| Clients | User/agent intent and typed requests | Supply trusted tenant authority |
| API Gateway | Contract validation, versioning, idempotency, routing | Bypass policy or invent memory semantics |
| Auth & Policy | Identity, grants, scope, deny-first machine rules | Treat retrieved text as authorization |
| Memory Domains | Domain-specific meaning and authority | Flatten user, agent, and organization truth together |
| Canonical Store | Immutable versions, current pointers, lifecycle, grants, outbox | Treat indexes as canonical truth |
| Workers & Indexes | Rebuildable enrichment, indexing, retries, purge work | Publish without revalidating policy and destination |
| Context Assembly | Authorized retrieval, canonical hydration, budgeting, citations | Return stale, revoked, deleted, or unauthorized content |
| Observability & Evaluation | Content-free telemetry and evidence-based promotion gates | Store raw protected content in ordinary telemetry |

## V1 boundary

The planned V1 is a modular API plus worker, PostgreSQL, and S3-compatible document storage. Redis, Kafka, Kubernetes, dedicated vector infrastructure, and graph infrastructure remain outside the active architecture until a measured evaluation justifies them.

Each milestone will add a focused before/after diagram for the component it changes. Those deeper diagrams must retain this visual language and keep the high-level source synchronized with verified system behavior.

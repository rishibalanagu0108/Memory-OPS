# M8 production evidence boundary

M8 verifies the implemented single-region modular API, worker, PostgreSQL, and object-storage
boundary. The synchronized final system view is the
[high-level architecture](../high-level-architecture.md), with editable Mermaid and a rendered SVG.

The protected full-scale run exercised 100 tenants, 1,000 active principals, 1,000,000 canonical
memories, 10,000 organizational documents, and 6,607 API requests over 3,739 seconds. It recorded
zero cross-tenant disclosures, zero post-revocation disclosures, and zero acknowledged-write loss.
The migration drill also proved upgrade and downgrade with zero data loss.

Production release remains **blocked**. Throughput, P95/P99 latency, index freshness, and five-minute
worker recovery missed their declared objectives. The 100 GiB source size was represented by
metadata rather than materialized object bytes, and multi-process/restarted-worker RSS growth was
not measured reliably. Cost evidence passed, but cost and hard-safety success cannot override those
failed readiness gates.

The binding evidence is in [`result.json`](../../../evals/m8/result.json), the final security review
is in [`security-review.json`](../../../evals/m8/security-review.json), and operator action is defined
by the [operations guide](../../operations/operations-guide.md).

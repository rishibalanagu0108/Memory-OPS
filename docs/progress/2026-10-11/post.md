# Memory-ops: a successful test can still say “do not ship”

M8 took Memory-ops through its first database-and-API scale run. The workload exercised 100 tenants,
1,000 active principals, 1,000,000 canonical memories, 10,000 organizational documents, and 6,607
requests over 3,739 seconds. Hard-safety observations stayed at zero for cross-tenant disclosure,
post-revocation disclosure, and acknowledged-write loss. Cost stayed inside the declared budget,
and a separate live-branch migration drill proved upgrade and rollback with zero data loss.

The production answer is still **NO-GO**.

Measured throughput was 1.767 QPS against a 25-QPS floor. P95 and P99 latency reached 54.3 and 67.2
seconds. Index lag exceeded 20 minutes, and 214 operations remained after the five-minute recovery
window. The 100 GiB source profile was metadata-represented rather than materialized in object
storage, while multi-process and restarted-worker memory growth was not measured reliably.

That distinction matters: the evaluation executed successfully, but the system did not satisfy its
production contract. The final architecture, security review, and operations guide now keep those
facts together. Passing safety and cost controls are preserved; failed readiness gates remain
visible and block release.

The lasting lesson is that evidence is most useful when it can stop a launch.

![M8 evidence to release decision](diagram.svg)

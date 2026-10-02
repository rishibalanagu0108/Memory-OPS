# Memory systems need stronger foundations than retrieval

The first milestone of Memory-ops is not a vector search demo. It is the boundary that makes
future memory operations trustworthy.

## The problem

A useful memory service must prevent cross-tenant disclosure, reject compromise-enabling
secrets, survive asynchronous worker failure, and avoid duplicating mutations when clients
retry. Retrieval quality does not compensate for failures in those foundations.

## What changed

M0 now has a runnable FastAPI/OpenAPI contract, explicit identity schemas, deny-by-default
authorization, PostgreSQL row-level security, atomic idempotency receipts, a transactional
outbox, retryable worker claims, and content-free audit metadata.

## Why this shape

PostgreSQL remains the canonical boundary. A write and its outbox notification commit in one
transaction, while derived work remains asynchronous and rebuildable. Native row-level
security provides defense in depth beneath application authorization without introducing a
new distributed system.

## Verified evidence

The M0 gate runs deterministic API, security, tenant-isolation, idempotency, outbox, and audit
holdout checks. Hard safety metrics require zero cross-tenant leakage, zero prohibited-secret
acceptance, zero duplicate mutations, zero acknowledged-write outbox loss, and zero raw-content
audit leakage. Performance is reported as a local baseline rather than an unsupported
production claim.

## Lesson learned

The durable queue is not an optimization. It is part of the write contract: success means both
canonical state and the work needed to rebuild derivatives are safely committed.

## Next step

Provision Neon as the managed staging PostgreSQL boundary, then begin M1’s explicit memory
semantics and canonical model while keeping Docker PostgreSQL for local development.

The accompanying diagram distinguishes verified M0 behavior from future memory capabilities.

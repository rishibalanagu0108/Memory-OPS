# From a safe database boundary to explicit memory

Memory-ops M1 is now implemented and evaluated.

The milestone adds an explicit `remember` path, immutable canonical versions, scoped `inspect`
and `list` reads, operation status, and typed Python and TypeScript clients. Writes remain
idempotent and commit their outbox notification in the same PostgreSQL transaction.

The important part was not merely making an endpoint return `201`. The implementation was
checked for all five semantic types, exact canonical round trips, tenant and subject isolation,
rollback on outbox failure, identical and conflicting retries, and prohibited-secret rejection.

The release holdout requires perfect semantic, round-trip, and scope rates, with zero accepted
secrets, cross-tenant disclosures, duplicate logical memories, duplicate versions, partial
commits, or acknowledged write loss. It uses no paid external model calls.

One practical lesson: mock SDK tests are useful but insufficient. Real Neon round trips exposed
an evidence-type mismatch in the Python client before release, which was corrected and covered
with a regression test.

Next, Memory-ops can move into temporal correction, conflict handling, expiration, and deletion
without weakening the canonical boundary established here.

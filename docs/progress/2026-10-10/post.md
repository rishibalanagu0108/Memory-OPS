# Memory-ops: retrieved documents are evidence, not instructions

M6 adds organizational knowledge without turning a document store into an authority bypass.

Uploads now preserve immutable document versions, structure-aware chunks, content hashes, and exact
source locators. Retrieval filters by tenant, workspace, the principal's current ACL grant, active
lifecycle, current version, publication window, and projection generation before ranking. Returned
text stays explicitly untrusted, while citations bind each passage to its document version, chunk,
policy revision, and locator.

The protected holdout covered parsing, retrieval, temporal source selection, conflicting policies,
prompt injection, stale projections, exact citations, and cross-tenant denial. The candidate scored
1.0 on all six quality measures, with zero unauthorized passages, noncurrent passages, executed
document instructions, or fabricated citations. No external model or judge was used.

The important boundary is simple: retrieval can supply cited evidence, but machine-enforced policy
still decides what an agent may do.

![M6 knowledge-flow evidence](diagram.svg)

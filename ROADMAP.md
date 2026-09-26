# Evolution Roadmap

Development proceeds one evidence-backed version at a time. No version is currently implemented.

## V1 — Basic memory (planned)

- Store a four-field memory in process.
- Retrieve memories with deterministic substring matching.
- Test storing and retrieval.

**Current state:** the architecture and expected limitations are documented, but the behavior has not yet been implemented or proven.

## V2 — Memory admission

Introduce JEV only to decide whether an observation should be remembered. This version is justified when blindly storing every observation creates demonstrable noise.

## V3 — Update and contradiction

Evaluate whether new information should be kept, updated, invalidated, stored separately, or ignored. Preserve history when the experiment shows that overwriting loses useful context.

## V4 — Retrieval decisions

Let JEV influence which retrieved memories are actually useful. Add this when literal store-level matching returns distracting evidence.

## V5 — Memory types

Add episodic, semantic, procedural, or working memory categories only when differing behavior cannot be expressed cleanly by the existing model.

## V6 — Provenance and confidence

Track evidence, confidence, and challenges when a real decision requires more auditability than V1's `source` field provides.

## V7 — Memory lifecycle

Experiment with decay, expiration, reinforcement, archival, forgetting, and invalidation after unbounded or stale memory causes a measurable problem.

## V8 — Advanced retrieval

Consider embeddings, semantic or hybrid search, reranking, temporal retrieval, and graph relationships only after simpler retrieval is shown insufficient. Technology follows the observed failure.

## V9 — Multi-agent memory

Add ownership, isolation, shared memory, access policy, and cross-agent provenance when more than one real agent needs the layer.

## V10 — Production-oriented platform

Address operational concerns and specialized memory systems after earlier experiments establish stable requirements. Distributed infrastructure is explicitly outside the current scope.

## Discovery log

### Proposed V1 assumptions to test

- In-process retention is enough to validate the first interface, but cannot support memory across restarts.
- Substring search is predictable, but cannot retrieve semantically related wording.
- Automatic admission keeps V1 small, but will accumulate duplicates, noise, and contradictions.

Future version work should cite a reproduced limitation here before adding infrastructure.

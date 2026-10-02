# M1 explicit-memory architecture

Status: **planned; implementation has not started**.

![M1 explicit-memory architecture](m1-explicit-memory.svg)

The editable source is [`m1-explicit-memory.mmd`](m1-explicit-memory.mmd).

## Reading the diagram

- Blue components are foundations already delivered in M0.
- Gold components are the explicit-memory behavior planned for M1.
- Green components form the authoritative, tenant-isolated PostgreSQL state.
- Purple is durable asynchronous processing inherited from M0.
- Grey derivatives are deliberately deferred beyond M1.

An allowed `remember` request is validated and admitted before entering one PostgreSQL
transaction. That transaction reserves the idempotency key, creates or identifies the logical
memory, appends an immutable version, records scope and provenance, writes an outbox event, and
finalizes the receipt. The API acknowledges the write only after that transaction commits.

`inspect` and `list` do not depend on embeddings in M1. They query current canonical versions
through tenant row-level security and return structured content, provenance, and validity.

## M1 boundary

M1 adds explicit `remember`, `inspect`, and `list` behavior plus Python and TypeScript clients.
Correction, forgetting, automatic extraction, semantic retrieval, summaries, and graph
projections remain later milestones. The outbox preserves a durable path to those rebuildable
derivatives without putting them on the canonical write path.

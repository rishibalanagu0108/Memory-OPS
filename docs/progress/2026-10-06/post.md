# Deletion is not finished when the row disappears

Memory-ops M2 now has verified temporal correction, deterministic conflict handling, immediate
revocation, expiration, complete purge receipts, and a deletion-aware restore boundary.

The central lesson came from the restore drill. A valid point-in-time restore can faithfully
bring back data that was deleted later. Therefore restoration cannot be treated as “database is
available, open traffic.” The safe order is restore offline, replay the completed deletion
ledger, rerun every required purge target, verify zero resurrection, and only then serve reads.

The live drill used an isolated Neon branch and synthetic data. It intentionally restored one
forgotten memory, kept the serving gate closed, replayed its content-free tombstone, and measured
zero surviving deleted memories with deletion completeness equal to 1.

Another practical lesson: restore timestamps are safety inputs. Rounding a timestamp down by a
fraction of a second can select a state before the intended commit. The drill caught that edge
case, retried with a timestamp after the commit and before deletion, and then passed.

Today’s verified boundary is deliberately precise. Canonical versions and evidence are physically
deleted. The future keyword, vector, graph, summary, and cache stores are not implemented yet, so
their receipts currently confirm absence rather than invoking external adapters. Production
recovery automation and incident drills remain a later operations milestone.

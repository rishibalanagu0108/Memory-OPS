# Memory-ops: deletion safety met retrieval safety

Today moved Memory-ops through two boundaries: M2 lifecycle safety and M3 governed retrieval.

M2 verified immutable corrections, deterministic conflict handling, immediate revocation,
complete purge receipts, and a restore gate that replays deletion records before traffic. The
live Neon drill confirmed that an old backup can restore forgotten content—and that keeping the
service offline until re-purge prevents resurrection.

M3 then added scoped exact, filtered, PostgreSQL full-text, and versioned local-vector retrieval.
Ranked candidates are never trusted directly: context assembly rehydrates the current canonical
version, rechecks authorization and lifecycle, prioritizes constraints, enforces a token budget,
and abstains on insufficient or low-confidence evidence.

The protected M3 comparison passed every absolute quality and safety floor: 1.0 NDCG@10,
MRR@10, recall@10, abstention precision/recall, critical-constraint recall, budget compliance,
and provenance validity, with zero hard-safety failures and no external-model spend.

One result matters more than a clean headline: RRF was not promoted. Although fusion beat every
simpler baseline on ranking quality, its isolated overhead exceeded the predeclared relative
latency ratio. The release path therefore stays on keyword retrieval with a governed vector
fallback. A gate that says “not yet” is working exactly as intended.

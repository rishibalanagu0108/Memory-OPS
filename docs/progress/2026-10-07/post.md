# Memory-ops: a passing gate can still mean “do not promote”

Today completed M4’s governed extraction boundary: conversation-derived memories can be proposed
in shadow mode, reviewed by a human, and evaluated independently by semantic category. Policy
denials and explicit remember, correct, or forget operations always take precedence. Shadow and
review flows write nothing to canonical memory.

The promotion controller now requires source-bound holdout evidence, minimum sample size,
precision, recall, calibration, reviewer agreement, regression success, and explicit approval.
Fact, preference, goal, constraint, and episode can be enabled, paused, or rolled back separately.

The protected readiness holdout passed all fail-closed checks and the extraction suite recorded
zero hard-safety violations. But there is no configured production extractor, so there are no
honest quality or calibration measurements yet. The result is therefore deliberately
non-promotional: all five categories remain in shadow mode with zero canonical writes.

That distinction is the useful lesson: verifying the safety machinery is not the same as proving
model quality. A trustworthy gate should make missing evidence visible and keep the feature off.

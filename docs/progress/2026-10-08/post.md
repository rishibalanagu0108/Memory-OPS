# Memory-ops: category gates can pass independently

M4 now has complete measured evidence instead of a readiness-only result. An Azure OpenAI shadow
extractor classified 500 protected holdout utterances: 100 each for facts, preferences, goals,
constraints, and episodes. Rishik Kumar and Tarun independently reviewed every case and agreed on
all 500 labels.

Fact, goal, and episode met their precision, recall, calibration, Brier, and reviewer-agreement
thresholds. Preference and constraint achieved perfect precision and recall but each recorded
0.095 expected calibration error, above the 0.05 ceiling. Those two categories therefore remain
shadow-only while the three passing categories are eligible for reviewed use.

This is category-level eligibility, not automatic memory writing. Canonical persistence remains
disabled, and policy denial, prohibited-secret checks, and explicit user operations keep
precedence.

![M4-06 evaluation flow](diagram.svg)

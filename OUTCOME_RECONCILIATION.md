# Avoma native meeting outcome sync

The proposed persistent unresolved backlog has been removed. Both hourly
analysis/enrichment and the separate six-hour reconciliation use HOURS_BACK
(default 24). There is no historical activity seed, fixed November cutoff,
permanent backlog, or automated confirmation deadline.

The review candidate retains calendar UID/offer/time matching, unique lead
matching, complete source pagination, bounded native Close meeting-window reads,
pre-write identity/outcome checks and readback. Only recognized explicit Avoma
tags establish a native outcome. Existing terminal outcomes and qualification
protections remain intact. Uncertain cases stay unchanged; no messages or
confirmation tasks are sent automatically. No immutable ledger is changed.

Validation: python -m unittest test_first_meeting_outcome_rules test_avoma_identity_guards test_meeting_window test_avoma_outcome test_qualified_routing

These safety improvements remain on a review branch, pending deployment approval.

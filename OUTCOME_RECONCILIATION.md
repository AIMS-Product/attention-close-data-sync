# Close-driven Avoma outcome reconciliation

The hourly analysis/enrichment job retains its recent window. The separate
six-hour reconciliation and outcome-only entry point retain Anthony's exact 320
historical Lane 2 activities plus supported blank/Scheduled meetings from
November 1, 2026 onward. This fixed start never moves forward. Meetings never
expire because an Avoma tag arrived more than 24 hours later. The broader
organization history is deliberately outside this historical repair.

Native Close search uses cursors to find the unresolved backlog efficiently;
the exact historical activity IDs are read separately with validated ID filters. Reconciliation queries merged, date-only Avoma
windows of at most one day, follows every source page, and matches locally using
calendar event UID or contact/attendee email, offer family, and time. Calendar
identity resolves neighboring bookings; ambiguous matches remain review cases.
It also checks the complete lead meeting history, including terminal and future
bookings, to avoid reusing evidence and to distinguish cancellation/rescheduling.

Only recognized explicit Avoma outcome tags write native Close outcome_id.
A recording or analysis existing never establishes attendance by itself.
The writer re-reads meeting identity and native outcome immediately before PUT,
preserves terminal outcomes and verifies readback. Enrichment and qualification
remain separate from the outcome-only path. No immutable ledger is changed.

The report checkpoints source progress, verified decisions, responsible host and
a fixed meeting-end-plus-48-hour review deadline. Overdue review or source errors
return nonzero, rather than presenting an unresolved backlog as complete. Host
confirmation is required when neither Avoma nor the Zoom backup establishes an
outcome. Missing evidence does not imply Show/No Show or payout eligibility.

Validation: `python -m unittest test_first_meeting_outcome_rules test_close_outcome_backlog test_native_outcome_backlog test_avoma_outcome test_qualified_routing`.
Dispatch `avoma_outcome_reconcile.yml` with `dry_run=1` for read-only validation.
Rollback: restore the previous source revision; keep recorded native outcomes.

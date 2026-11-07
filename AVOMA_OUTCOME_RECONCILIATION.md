# Meeting outcome reconciliation

The outcome-only Avoma workflow runs four times per day with a 24-hour overlap. It re-reads eligible Avoma meetings and the corresponding blank or Scheduled Close meeting activities. The overlap catches late Avoma tags and makes reruns idempotent without a checkpoint.

## Coverage

- Avoma-tagged meetings for Vendingpreneurs, Acquisition Ace, TLG, PPA, Land Geek, and Publishing Profits are eligible. Vendingpreneurs next-steps, follow-up, canceled, and rescheduled titles remain in scope for outcomes; the first-sale-only filter still controls analysis enrichment and Qualified updates in the separate hourly workflow.
- The Avoma path maps explicit meeting-result tags to Completed, including `Decision Made` and `Follow-up Scheduled`; No Show, Rescheduled, and Cancelled remain separate outcomes. Unknown or missing tags go to the review artifact.
- Avoma matches by lead, offer family, and start time. It writes only after finding one candidate and re-reading the Close outcome immediately before the update. Existing terminal outcomes are preserved. A cancellation with a later same-offer booking is recorded as Rescheduled.
- The Close backup workflow also runs four times per day. It scans all meeting titles for the prior 24 hours after a three-hour grace period, uses Zoom and Close evidence, preserves terminal outcomes, and keeps Zoom-only auto-No-Show disabled. Unresolved meetings appear in its run report.

## Unresolved cases

The two workflows assign outcomes only when evidence supports them. Missing recordings, missing tags, weak attendance signals, or ambiguous matches remain in review; they are not converted to Completed or No Show by default. Avoma and Close review artifacts are retained for 90 days. A manual Avoma workflow dispatch defaults to dry-run and accepts a longer `hours_back` value for a historical reconciliation preview.

## Schedules

- Avoma reconciliation: 02:17, 08:17, 14:17, and 20:17 UTC.
- Close evidence backup: 00:37, 06:37, 12:37, and 18:37 UTC.

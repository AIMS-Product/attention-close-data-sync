# Qualification decision provenance

Every automated qualification write in the first-meeting, general meeting, compatibility and manual backfill entry points sends the decision, Qualified Reason, Qualified Source Call ID and Qualified Decision At in one Close lead update. Raw outcome labels remain distinguishable, including Disqualified, Lost and Not Interested. IDs are provider-prefixed (`avoma:<meeting UUID>`); filenames beginning with `attention_` in this repository currently consume the Avoma API and therefore also record `avoma:`. AI-inferred backfill decisions are labeled as inferred rather than native outcomes.

Existing qualification eligibility, override and unchanged-value guards remain. Missing reason/call references prevent the qualification write. An unsuccessful override read cannot be treated as a blank unlocked lead. A No-to-No replay does not move the decision date. Existing historical reasons are not invented or bulk-filled. Manual UI qualification edits and external integrations are not intercepted by these scripts.

Offline checks: `python3 -m unittest test_qualification_provenance test_qualified_routing test_first_meeting_outcome_rules`.

"""No decision is written without the reason and actual provider call ID."""
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
os.environ.setdefault('CLOSE_API_KEY','offline-test')
os.environ.setdefault('AVOMA_API_KEY','offline-test')
os.environ.setdefault('ATTENTION_API_KEY','offline-test')
os.environ.setdefault('ANTHROPIC_API_KEY','offline-test')
import qualification_provenance as provenance
import avoma_to_close_first_meeting_sync as avoma
import attention_to_close_first_meeting_sync as attention
import avoma_qualified_backfill as backfill
import avoma_to_close_meeting_sync as meeting_sync
import attention_to_close_meeting_sync as compatibility_sync

class QualificationProvenanceTests(unittest.TestCase):
    def test_payload_retains_raw_outcomes_and_provider_id(self):
        for provider in ('avoma','attention'):
            for label in ('Disqualified','Not Interested','Lost'):
                payload=provenance.qualification_payload('No',label,'source-id',provider=provider,decided_at=datetime(2026,11,17,21,tzinfo=timezone.utc))
                self.assertEqual(payload['custom.'+provenance.REASON_FIELD],provider+': '+label)
                self.assertEqual(payload['custom.'+provenance.CALL_ID_FIELD],provider+':source-id')
                self.assertEqual(payload['custom.'+provenance.DECISION_AT_FIELD],'2026-11-17T21:00:00+00:00')
    def test_missing_reference_or_reason_fails_closed(self):
        for reason,call in [('', 'id'),('Lost', None)]:
            with self.assertRaises(ValueError):provenance.qualification_payload('No',reason,call,provider='avoma')
    def test_writers_issue_one_atomic_put(self):
        for sync,provider in [(avoma,'avoma'),(attention,'avoma')]:
            with self.subTest(provider=provider),patch.object(sync,'DRY_RUN',False),patch.object(sync,'get_lead_overrides',return_value={'qualified_override':None,'qualified_current':None}),patch.object(sync,'close_put',return_value=SimpleNamespace(ok=True)) as put:
                sync.update_lead_qualified('lead_fixture','Disqualified','actual-call-id')
                payload=put.call_args.args[1]
                self.assertEqual(len(payload),4)
                self.assertEqual(payload['custom.'+provenance.QUALIFIED_FIELD],'No')
                self.assertEqual(payload['custom.'+provenance.CALL_ID_FIELD],provider+':actual-call-id')
                self.assertEqual(put.call_count,1)
    def test_override_existing_decision_and_missing_call_never_write(self):
        for sync in (avoma,attention):
            for state in ({'qualified_override':'Yes','qualified_current':None},{'qualified_override':None,'qualified_current':'No'},{'qualified_override':None,'qualified_current':None}):
                with patch.object(sync,'DRY_RUN',False),patch.object(sync,'get_lead_overrides',return_value=state),patch.object(sync,'close_put') as put:
                    sync.update_lead_qualified('lead_fixture','Lost',None)
                    put.assert_not_called()
    def test_guard_read_failure_does_not_become_unlocked_blank(self):
        for sync in (avoma,attention):
            with patch.object(sync,'close_get',return_value=SimpleNamespace(ok=False)),patch.object(sync,'close_put') as put:
                with self.assertRaises(RuntimeError):sync.update_lead_qualified('lead_fixture','Lost','call-id')
                put.assert_not_called()
    def test_inferred_backfill_remains_labeled(self):
        with patch.object(backfill,'DRY_RUN',False),patch.object(backfill,'close_put',return_value=SimpleNamespace(ok=True)) as put:
            backfill.write_lead_qualified('lead_fixture','No','AI-inferred Disqualified (Avoma notes)','id')
            self.assertIn('AI-inferred',put.call_args.args[1]['custom.'+provenance.REASON_FIELD])

    def test_followup_transition_writes_metadata_and_repeat_does_not_redate(self):
        for sync in (meeting_sync, compatibility_sync):
            for previous,expected_calls in [('Yes',1),('No',0)]:
                with patch.object(sync,'DRY_RUN',False),patch.object(sync,'get_lead_qualified_override',return_value={'qualified_override':None,'qualified_current':previous}),patch.object(sync,'close_put',return_value=SimpleNamespace(ok=True)) as put:
                    sync.update_lead_qualified('lead_fixture','Lost',source_call_id='followup-uuid')
                    # Compatibility writer preserves any existing value; active writer allows requalification.
                    if sync is compatibility_sync:expected_calls=0
                    self.assertEqual(put.call_count,expected_calls)
                    if expected_calls:self.assertEqual(put.call_args.args[1]['custom.'+provenance.CALL_ID_FIELD],'avoma:followup-uuid')
    def test_compatibility_blank_write_has_metadata(self):
        with patch.object(compatibility_sync,'DRY_RUN',False),patch.object(compatibility_sync,'get_lead_qualified_override',return_value={'qualified_override':None,'qualified_current':None}),patch.object(compatibility_sync,'close_put',return_value=SimpleNamespace(ok=True)) as put:
            compatibility_sync.update_lead_qualified('lead_fixture','Lost','compatibility-avoma-uuid')
            self.assertEqual(put.call_args.args[1]['custom.'+provenance.CALL_ID_FIELD],'avoma:compatibility-avoma-uuid')

if __name__=='__main__':unittest.main()

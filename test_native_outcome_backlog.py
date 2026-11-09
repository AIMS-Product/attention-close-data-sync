import json,unittest
from datetime import datetime,timezone
from pathlib import Path
from close_outcome_backlog import read_outcome_backlog,backlog_query,COVERAGE_START,confirmation_due_at

NOW=datetime(2026,11,9,tzinfo=timezone.utc)

class BacklogReaderTests(unittest.TestCase):
    def test_queries_have_fixed_coverage_start_not_rolling_expiry(self):
        query=json.dumps(backlog_query(NOW))
        self.assertIn(COVERAGE_START,query)
        self.assertIn('outcome_id',query)

    def test_historical_cleanup_has_fixed_48_hour_confirmation_deadline(self):
        self.assertEqual("2026-11-11T17:00:00+00:00", confirmation_due_at(datetime(2026,1,1,tzinfo=timezone.utc)).isoformat())
        self.assertEqual("2026-11-12T00:00:00+00:00", confirmation_due_at(datetime(2026,11,10,tzinfo=timezone.utc)).isoformat())


    def test_cursor_pages_and_historical_seed_are_merged(self):
        calls=[]
        def search(payload):
            calls.append(payload)
            return {'data':[{'id':'recent'}],'cursor':'next' if len(calls)==1 else None,'count':{'total':1}}
        result=read_outcome_backlog(search,lambda params:{'data':[]},'id',NOW)
        self.assertEqual([{'id':'recent'}],result)
        self.assertEqual('next',calls[1]['cursor'])

    def test_cursor_repeat_or_oversized_result_fails(self):
        for response in [{'data':[],'cursor':'same'},{'data':[],'count':{'total':10001}}]:
            with self.assertRaises(RuntimeError):
                read_outcome_backlog(lambda payload:response,lambda p:{'data':[]},'id',NOW)

    def test_missing_seed_filter_or_partial_page_fails(self):
        for response in [{'data':[{'id':'unrelated'}]},{'data':[],'has_more':True}]:
            with self.assertRaises(RuntimeError):
                read_outcome_backlog(lambda p:{'data':[]},lambda p:response,'id',NOW)

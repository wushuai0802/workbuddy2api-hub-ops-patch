"""Compaction terminals, original billing and account-slot lifetime."""
import io
import json
import time
import unittest
import urllib.error
from unittest import mock

from test_integration import Account, BODY, MODEL, Handler, Stream, chunk, turn, wb
from test_guarded_sse_batching import ChunkSource
from test_v1617 import MigrationTests
from test_v1710 import PAID


class Migration30Tests(MigrationTests):
    def compact(self, source, stream=False):
        h=Handler('/v1/responses');h.key_entry={'id':'compaction-test-key'}
        wb.Handler._responses_compaction_response(h,source,MODEL,{'hash':'synthetic'},
            Account('first'),time.time(),stream,base_body=BODY,effort='high')
        return h

    def test_successful_compaction_preserves_billed_model_and_provider_usage(self):
        for stream in (False,True):
            with self.subTest(stream=stream):
                self.records.clear()
                source=ChunkSource([b''.join(turn({'content':'handoff summary'},tokens=53))])
                source._wb_billed_model=PAID
                h=self.compact(source,stream)
                if stream:
                    events=[json.loads(l[6:])for l in b''.join(h.written).decode().splitlines()if l.startswith('data: {')]
                    self.assertEqual(events[-1]['type'],'response.completed')
                    item=events[-1]['response']['output'][0]
                else:item=h.json_body['output'][0]
                self.assertEqual(item['type'],'compaction')
                self.assertEqual(wb._decode_compaction_summary(item['encrypted_content']),'handoff summary')
                self.assertEqual(self.records[0][1][0],PAID)
                self.assertEqual(self.records[0][1][1]['total_tokens'],53)
                self.assertEqual(self.records[0][2]['key'],'compaction-test-key')
                self.assertEqual(self.records[0][2]['effort'],'high')
                self.assertTrue(source.closed)

    def test_partial_filtered_and_error_summaries_never_replace_history(self):
        for stream in (False,True):
            for finish in ('length','content_filter','error','unexpected',None):
                with self.subTest(stream=stream,finish=finish):
                    self.records.clear()
                    raw=turn({'content':'PARTIAL'},finish=finish,tokens=53)
                    source=ChunkSource([b''.join(raw)])
                    h=self.compact(source,stream)
                    self.assertTrue(h.json_body.get('error'))
                    self.assertFalse(h.json_body.get('output'))
                    self.assertFalse(h.written)
                    self.assertEqual(self.records[-1][0],'error')
                    self.assertEqual(self.records[-1][2]['usage']['total_tokens'],53)

    def test_refusal_or_tool_bearing_summary_is_not_valid_compaction(self):
        for delta in ({'content':'text','refusal':'denied'}, {'content':'text','tool_calls':[{'index':0,'id':'call','function':{'name':'side_effect','arguments':'{}'}}]}):
            h=self.compact(ChunkSource([b''.join(turn(delta,'tool_calls' if 'tool_calls'in delta else 'stop'))]))
            self.assertTrue(h.json_body.get('error'))

    def test_compaction_read_failure_preserves_billing_and_consumed_usage(self):
        class Broken(ChunkSource):
            def read1(self,size):
                if not self.chunks:raise ConnectionResetError('synthetic upstream reset')
                return super().read1(size)
        source=Broken([chunk({'content':'partial'})+chunk(usage={'prompt_tokens':40,'completion_tokens':13,'total_tokens':53})])
        source._wb_billed_model=PAID
        h=self.compact(source)
        self.assertTrue(h.json_body.get('error'))
        self.assertEqual(self.records[-1][1][0],PAID)
        self.assertEqual(self.records[-1][2]['usage']['total_tokens'],53)

    def test_compaction_inherits_generation_deadline_before_reading(self):
        source=ChunkSource([b''.join(turn({'content':'must not read'}))])
        source._ops_generation_deadline=time.monotonic()-1
        h=self.compact(source)
        self.assertTrue(h.json_body.get('error'))
        self.assertEqual(source.reads,0)

    def test_guarded_compaction_releases_account_slot_once(self):
        account=Account('slot-owner')
        with mock.patch.dict(wb._ACCOUNT_INFLIGHT,{},clear=True):
            for finish in ('stop','error'):
                source=ChunkSource([b''.join(turn({'content':'summary'},finish))])
                self.assertTrue(wb.account_slot_acquire(account,limit=1))
                wb.bind_account_slot_to_response(source,account)
                self.compact(source)
                self.assertNotIn(account.uid,wb._ACCOUNT_INFLIGHT)
                source.close();self.assertNotIn(account.uid,wb._ACCOUNT_INFLIGHT)

    def test_static_key_request_refresh_never_marks_it_expired(self):
        a=wb.wb_accounts.Account({'uid':'static-key-fixture','realm':'cn','accessToken':'ck_synthetic_key','source':wb.wb_accounts.SOURCE_API_KEY})
        with mock.patch.object(wb.wb_accounts,'http_json')as opened:
            self.assertFalse(a.refresh_after_401(a.access_token,deadline_at=time.monotonic()+1))
            self.assertFalse(a.token_expired)
            opened.assert_not_called()

    def test_nonretryable_400_releases_reserved_account_slot(self):
        a=wb.wb_accounts.Account({'uid':'slot-test','realm':'intl','accessToken':'synthetic-access'})
        pool=mock.Mock();pool.accounts=[a];pool.count_ready.return_value=1;pool.pick_for_session.return_value=a
        err=urllib.error.HTTPError('https://example.invalid',400,'invalid',{},io.BytesIO(b'{"code":11133}'))
        with mock.patch.dict(wb._ACCOUNT_INFLIGHT,{},clear=True),mock.patch.object(wb,'POOL',pool), \
             mock.patch.object(wb,'apply_daily_guards'),mock.patch.object(wb.wb_settings,'account_max_concurrent',return_value=1), \
             mock.patch.object(wb.wb_accounts,'urlopen',side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):wb.open_upstream(BODY)
            self.assertNotIn(a.uid,wb._ACCOUNT_INFLIGHT)


if __name__=='__main__':
    suite=unittest.TestSuite(Migration30Tests(n)for n in Migration30Tests.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

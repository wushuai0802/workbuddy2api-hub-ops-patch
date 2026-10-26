"""Early DS failures are visible without duplicate accounting or changed request bodies."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from test_integration import Account,MODEL,turn,wb
from test_guarded_sse_batching import ChunkSource


class ResponseSource(ChunkSource):
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


class EntryObservationTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ['WB_OPS_TEST_STATE'])))
        (self.root/'accounts').mkdir();(self.root/'usage').mkdir()
        self.store=wb.wb_observe.ObservationStore(self.root/'observations.jsonl')
        self.enterContext(mock.patch.dict(os.environ,{'WB_OPS_OBSERVE':'1','WB_OPS_LOOP_RETRY':'0'}))
        self.enterContext(mock.patch.multiple(wb,POOL=None,USAGE_DIR=str(self.root/'usage'),USAGE_LOG=str(self.root/'usage/usage.jsonl'),ACCOUNTS_DIR=str(self.root/'accounts'),BLOCK_BACKGROUND_REQUESTS=False))
        self.enterContext(mock.patch.object(wb,'_observation_store',return_value=self.store))
        self.enterContext(mock.patch.object(wb,'log'))
        self.enterContext(mock.patch.object(wb,'extract_session_key',return_value='PRIVATE_SESSION'))
        self.enterContext(mock.patch.object(wb.wb_pricing,'current_policy_id',return_value='fixture-policy'))

    def invoke(self,protocol,error=None):
        h=wb.Handler.__new__(wb.Handler)
        h.path='/v1/'+protocol;h.headers={};h.client_address=('127.0.0.1',1)
        h.key_entry={'id':'PRIVATE_KEY','key':'PRIVATE_SECRET','realm':'intl'}
        h._key_id=mock.Mock(return_value='PRIVATE_KEY')
        for name in ('_cross_realm_error','_banned_model_error','_key_model_error'):setattr(h,name,mock.Mock(return_value=None))
        h._request_realm=mock.Mock(return_value='intl')
        h.status=None
        def reply(status,*args,**kwargs):h.status=status
        h._error=reply;h._anthropic_error=reply
        h._rate_limited=lambda exc:reply(429,exc.message or exc.detail)
        h._anthropic_rate_limited=h._rate_limited
        h.send_response=reply;h.send_header=mock.Mock();h.end_headers=mock.Mock();h.wfile=io.BytesIO()
        payload={'model':MODEL,'stream':True,'messages':[{'role':'user','content':'PRIVATE_PROMPT'}],'max_tokens':32}
        if protocol=='responses':payload['input']=payload.pop('messages')
        before=copy.deepcopy(payload)
        source=ResponseSource([b''.join(turn({'content':'synthetic answer'},tokens=93))])
        with mock.patch.object(wb,'_open_first_stream',side_effect=error,return_value=(source,Account('PRIVATE_ACCOUNT'),None))as opened:
            if protocol=='messages':h._handle_messages(payload)
            else:h._dispatch_chat_post('/v1/'+('responses'if protocol=='responses'else'chat/completions'),payload)
        self.assertEqual(payload,before)
        self.assertEqual(opened.call_count,1)
        return h

    def test_restriction_and_pause_are_observed_before_response_in_all_protocols(self):
        cases=((lambda:wb.ContentRejected(detail='{"code":11140,"msg":"PRIVATE_DETAIL"}'),403,'upstream_account_restricted'),
               (lambda:wb.RateLimited(detail='upstream account restriction in effect',message='dispatch paused 90s / code 11140'),429,'upstream_restriction_pause'),
               (lambda:wb.RateLimited(detail='usage exceeds frequency limit / 6004'),429,'upstream_rate_limited'))
        for protocol in ('chat','responses','messages'):
            for factory,status,reason in cases:
                with self.subTest(protocol=protocol,reason=reason):
                    before=len(self.store.snapshot()['recent'])
                    h=self.invoke(protocol,factory())
                    self.assertEqual(h.status,status)
                    rows=self.store.snapshot()['recent'][before:]
                    self.assertEqual([r['phase']for r in rows],['attempt','request'])
                    self.assertTrue(all(r['stage']=='entry'and r['reason']==reason for r in rows))
                    self.assertEqual(rows[-1]['protocol'],protocol)
                    self.assertTrue(rows[-1]['session_present'])
                    self.assertEqual(rows[-1]['outcome'],'failed')
                    self.assertNotIn('PRIVATE_',json.dumps(rows))

    def test_no_usable_account_is_503_in_http_and_usage_ledger(self):
        for protocol in ('chat','responses','messages'):
            with self.subTest(protocol=protocol):
                h=self.invoke(protocol,RuntimeError('no usable account: synthetic cooling pool'))
                self.assertEqual(h.status,503)
                row=json.loads((self.root/'usage/usage.jsonl').read_text().splitlines()[-1])
                self.assertEqual(row['status'],503)
                self.assertEqual(self.store.snapshot()['recent'][-1]['reason'],'no_account_available')

    def test_successful_nested_entry_emits_one_request_and_one_usage_row(self):
        for protocol in ('chat','responses','messages'):
            with self.subTest(protocol=protocol):
                before=len(self.store.snapshot()['recent'])
                h=self.invoke(protocol)
                self.assertEqual(h.status,200)
                rows=self.store.snapshot()['recent'][before:]
                self.assertEqual([r['phase']for r in rows],['attempt','request'])
                self.assertEqual(rows[-1]['outcome'],'completed')
                self.assertEqual(rows[-1]['stage'],'response')
                self.assertEqual(rows[-1]['usage']['total_tokens'],93)
                self.assertEqual(rows[-1]['protocol'],protocol)
                self.assertTrue(rows[-1]['session_present'])
        self.assertEqual(len((self.root/'usage/usage.jsonl').read_text().splitlines()),3)

    def test_disabled_observation_keeps_http_and_ledger_without_observation_rows(self):
        with mock.patch.dict(os.environ,{'WB_OPS_OBSERVE':'0'}):
            h=self.invoke('responses',wb.ContentRejected(detail='{"code":11140}'))
        self.assertEqual(h.status,403)
        self.assertEqual(self.store.snapshot()['recent'],[])
        self.assertEqual(len((self.root/'usage/usage.jsonl').read_text().splitlines()),1)


if __name__=='__main__':unittest.main()

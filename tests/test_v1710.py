"""Error terminals, actual billed identity, policy controls and key slots."""
import copy
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_integration import Account,BODY,Handler,MODEL,Stream,chunk,responses,turn,wb
from test_guarded_sse_batching import ChunkSource
from test_loop_recovery import LOOP
from test_v172 import DSML

PAID='deepseek-v4.1-flash-sg'
USAGE={'prompt_tokens':21,'completion_tokens':10,'total_tokens':31,
       'prompt_tokens_details':{'cached_tokens':7},'completion_tokens_details':{'reasoning_tokens':3}}
APIS=[(p,s) for p in ('chat','responses','messages') for s in (True,False)]


class TerminalAndBillingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ,{'WB_OPS_OBSERVE':'0','WB_OPS_LOOP_RETRY':'0','WB_OPS_LOOP_RECOVERY_HINT':'1'}))
        self.enterContext(mock.patch.object(wb,'apply_daily_token_limit'))
        self.enterContext(mock.patch.object(wb,'log'))

    def invoke(self,protocol,stream,frames,billed=None,probed=False,writer_error=None,replacement=None):
        h=Handler('/v1/'+protocol,writer_error=writer_error)
        source=ChunkSource([b''.join(frames)])
        if billed:source._wb_billed_model=billed
        upstream=wb.probe_first_frame(source)[0] if probed else source
        args=[upstream,MODEL]
        if protocol=='responses':args += [set(),{}]
        args += [{},Account(),time.time()]
        rows=[]
        def usage(model,value,**kwargs):rows.append({'type':'usage','model':model,'usage':value,**kwargs})
        def error(model,status,message,**kwargs):rows.append({'type':'error','model':model,'status':status,'message':message,**kwargs})
        options={'return_value':replacement} if replacement is not None else {'side_effect':AssertionError('unexpected model replay')}
        with mock.patch.object(wb,'record_usage',side_effect=usage),mock.patch.object(wb,'record_error',side_effect=error),mock.patch.object(wb,'open_upstream',**options) as opened:
            getattr(h,'_'+protocol+('_stream_response' if stream else '_nonstream_response'))(*args,base_body=BODY)
        self.assertTrue(source.closed)
        return h,rows,source,opened.call_count

    def error_turn(self,delta=None):
        return [chunk(delta or {},finish='error',usage=USAGE),b'data: [DONE]\n\n']

    def assert_error(self,h,rows,stream):
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['type'],'error')
        self.assertEqual(rows[0]['status'],502)
        self.assertEqual(rows[0]['usage'],USAGE)
        self.assertNotEqual(rows[0].get('outcome','failed'),'completed')
        value=json.dumps(responses(h) if stream else h.json_body)
        self.assertIn('upstream_finish_error',value)
        self.assertNotIn('response.completed',value)

    def test_error_terminal_with_same_frame_content_fails_six_paths_once_without_replay(self):
        for p,s in APIS:
            with self.subTest(protocol=p,stream=s):
                h,rows,source,opens=self.invoke(p,s,self.error_turn({'content':'Visible partial output.'}),probed=s)
                self.assert_error(h,rows,s)
                self.assertEqual(opens,0)
                self.assertEqual(source.reads,1)
                if s:self.assertIn('Visible partial output.',json.dumps(responses(h)))

    def test_empty_reasoning_and_refusal_error_never_recover(self):
        for delta in ({},{'reasoning_content':'Unfinished analysis.'},{'refusal':'Cannot complete.'}):
            for p,s in APIS:
                with self.subTest(delta=tuple(delta),protocol=p,stream=s),mock.patch.dict(os.environ,{'WB_OPS_LOOP_RETRY':'1'}):
                    h,rows,_,opens=self.invoke(p,s,self.error_turn(delta))
                    self.assert_error(h,rows,s);self.assertEqual(opens,0)

    def test_failed_printed_tool_call_is_not_synthesized_six_paths(self):
        for p,s in APIS:
            with self.subTest(protocol=p,stream=s):
                h,rows,_,opens=self.invoke(p,s,[chunk({'content':DSML})]+self.error_turn())
                self.assert_error(h,rows,s);self.assertEqual(opens,0)
                value=json.dumps(responses(h) if s else h.json_body)
                self.assertNotIn('"type": "function_call"',value)
                self.assertNotIn('"finish_reason": "tool_calls"',value)

    def test_prefetched_tail_usage_after_error_is_preserved_without_draining(self):
        frames=[chunk({'content':'Partial.'},finish='error'),chunk(usage=USAGE),b'data: [DONE]\n\n']
        for p,s in APIS:
            with self.subTest(protocol=p,stream=s):
                h,rows,source,_=self.invoke(p,s,frames,probed=s)
                self.assert_error(h,rows,s);self.assertEqual(source.reads,1)

    def test_actual_billed_model_survives_probe_success_and_error_six_paths(self):
        for error in (False,True):
            frames=self.error_turn({'content':'Partial.'}) if error else [chunk({'content':'Answer.'}),chunk(finish='stop',usage=USAGE),b'data: [DONE]\n\n']
            for p,s in APIS:
                with self.subTest(error=error,protocol=p,stream=s):
                    h,rows,_,_=self.invoke(p,s,frames,billed=PAID,probed=s)
                    self.assertEqual(len(rows),1)
                    self.assertEqual(rows[0]['model'],PAID)
                    self.assertEqual(rows[0]['requested_model'],MODEL)
                    self.assertEqual(rows[0]['usage'],USAGE)
                    if error:self.assert_error(h,rows,s)

    def test_billing_resets_per_attempt_when_safe_recovery_changes_model(self):
        for p,s in APIS:
            with self.subTest(protocol=p,stream=s),mock.patch.dict(os.environ,{'WB_OPS_LOOP_RETRY':'1'}):
                replacement=Stream([chunk({'content':'Recovered answer.'}),chunk(finish='stop',usage=USAGE),b'data: [DONE]\n\n'])
                replacement._wb_billed_model=PAID
                h,rows,_,opens=self.invoke(p,s,[chunk({'reasoning_content':LOOP})],replacement=(replacement,Account('b'),None))
                self.assertEqual(opens,1)
                self.assertEqual(len(rows),2)
                self.assertEqual([r['model'] for r in rows],[MODEL,PAID])
                self.assertEqual(rows[-1]['requested_model'],MODEL)
                self.assertEqual(rows[-1]['usage'],USAGE)

    def test_error_reason_and_signal_are_persisted_in_request_observation(self):
        captured=[]
        h=Handler('/v1/chat/completions')
        with mock.patch.dict(os.environ,{'WB_OPS_OBSERVE':'1'}),mock.patch.object(wb.wb_observe.ObservationStore,'append',side_effect=lambda rows:captured.extend(rows)),mock.patch.object(wb,'_persist_usage'):
            h._chat_stream_response(Stream(self.error_turn({'content':'Partial.'})),MODEL,{},Account(),time.time(),base_body=BODY)
        request=next(row for row in captured if row['phase']=='request')
        self.assertEqual(request['reason'],'upstream_finish_error')
        self.assertEqual(request['signals']['upstream_finish_error'],1)
        self.assertNotEqual(request['outcome'],'completed')

    def test_probe_failure_is_accounted_with_billed_model_before_holder_creation(self):
        for p in ('chat','responses'):
            with self.subTest(protocol=p),mock.patch.object(wb,'probe_first_frame',side_effect=TimeoutError('synthetic probe expiry')):
                _,rows,_,opens=self.invoke(p,True,turn({'content':'unread'}),billed=PAID)
            self.assertEqual(opens,0);self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['model'],PAID);self.assertEqual(rows[0]['requested_model'],MODEL)
            self.assertEqual(rows[0]['status'],502)


class PolicyAndSlotTests(unittest.TestCase):
    def handler(self,path='/v1/chat/completions',dispatch=None):
        h=wb.Handler.__new__(wb.Handler);h.path=path;h.key_entry={'id':'synthetic-key','concurrency_limit':1}
        h._is_panel_route=mock.Mock(return_value=False);h._authorized=mock.Mock(return_value=True);h._key_ok=mock.Mock(return_value=True)
        h._payload_or_error=mock.Mock(return_value=dict(BODY));h._error=mock.Mock();h._anthropic_error=mock.Mock()
        h._dispatch_chat_post=dispatch or mock.Mock();h._handle_messages=dispatch or mock.Mock()
        return h

    def test_key_slot_is_released_on_dispatch_exception_and_global_saturation(self):
        for saturation in (False,True):
            h=self.handler(dispatch=mock.Mock(side_effect=ConnectionResetError('synthetic cancel')))
            slots=mock.Mock();slots.acquire.return_value=not saturation
            with mock.patch.object(wb,'_chat_slots',slots),mock.patch.object(wb,'record_error'):
                if saturation:h.do_POST()
                else:
                    with self.assertRaises(ConnectionResetError):h.do_POST()
            self.assertNotIn('synthetic-key',wb._key_active_concurrency)

    def test_real_http_concurrent_request_is_rejected_then_slot_becomes_reusable(self):
        entered=threading.Event();release=threading.Event();result=[]
        class Gateway(wb.Handler):
            def _authorized(self):self.key_entry={'id':'synthetic-http-key','name':'synthetic','concurrency_limit':1};return True
            def _dispatch_chat_post(self,path,payload):entered.set();release.wait(3);return self._json(200,{'ok':True})
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Gateway);server.daemon_threads=True
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        def call():
            client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            try:
                client.request('POST','/v1/chat/completions',json.dumps(BODY),{'Content-Type':'application/json'})
                response=client.getresponse();value=json.loads(response.read());return response.status,value
            finally:client.close()
        first=threading.Thread(target=lambda:result.append(call()),daemon=True)
        try:
            with mock.patch.object(wb,'record_error'),mock.patch.object(wb,'open_upstream',side_effect=AssertionError('forbidden model call')):
                first.start();self.assertTrue(entered.wait(2))
                status,value=call();self.assertEqual(status,429);self.assertEqual(value['error']['type'],'rate_limit_error')
                release.set();first.join(5);self.assertFalse(first.is_alive());self.assertEqual(result[0][0],200)
                self.assertEqual(call()[0],200)
            self.assertNotIn('synthetic-http-key',wb._key_active_concurrency)
        finally:release.set();first.join(5);server.shutdown();server.server_close();worker.join(3)

    def test_panel_controls_messages_after_legacy_environment_is_cleared(self):
        with tempfile.TemporaryDirectory(dir=os.environ['WB_OPS_TEST_STATE']) as directory,mock.patch.object(wb,'ACCOUNTS_DIR',directory),mock.patch.dict(os.environ,{'WB_ENABLE_ANTHROPIC_MESSAGES':''}):
            wb.wb_settings.set_enable_anthropic_messages(directory,True);self.assertTrue(wb._anthropic_messages_enabled())
            wb.wb_settings.set_enable_anthropic_messages(directory,False);self.assertFalse(wb._anthropic_messages_enabled())
            with mock.patch.dict(os.environ,{'WB_ENABLE_ANTHROPIC_MESSAGES':'1'}):self.assertTrue(wb._anthropic_messages_enabled())

    def test_credit_fallback_preserves_original_deadline_exclusions_and_input(self):
        account=mock.Mock();account.uid='synthetic';account.realm='intl';account.enabled=True;account.access_token='synthetic';account.cap_until.return_value=time.time()+600
        pool=mock.Mock();pool.accounts=[account];pool.count_ready.return_value=0;pool.pick_for_session.return_value=None
        original=wb.open_upstream;body=copy.deepcopy(BODY);before=copy.deepcopy(body);deadline=time.monotonic()+10
        response=Stream(turn({'content':'Answer.'}));calls=[]
        def paid(payload,**kwargs):calls.append((copy.deepcopy(payload),kwargs));return response,Account(),None
        with mock.patch.object(wb,'POOL',pool),mock.patch.object(wb,'apply_daily_guards'),mock.patch.object(wb,'remaining_priority_enabled',return_value=False),mock.patch.object(wb,'_free_model_credit_fallback_enabled',return_value=True),mock.patch.object(wb,'open_upstream',side_effect=paid),mock.patch.object(wb,'log'):
            out,_,_=original(body,target_realm='intl',session_key='synthetic-session',exclude_uids=['excluded'],deadline_at=deadline)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][0]['model'],PAID)
        self.assertEqual(calls[0][1]['deadline_at'],deadline);self.assertEqual(calls[0][1]['exclude_uids'],['excluded'])
        self.assertFalse(calls[0][1]['allow_credit_fallback']);self.assertFalse(calls[0][1]['allow_product_switch'])
        self.assertEqual(body,before);self.assertEqual(out._wb_billed_model,PAID);out.close()


if __name__=='__main__':unittest.main()

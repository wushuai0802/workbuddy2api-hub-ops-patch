# Synthetic cases adapted from the supplied v1.10.1 / 20261102-r2 reference.
"""Provider isolation, lease ownership and optional transport behavior."""
import io
import http.client
import socket
import faulthandler
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock
from test_integration import wb, Handler, MODEL, BODY, chunk, turn, IntegrationTests, REAL_RECORD
from test_account_slot_lifecycle import APIS
from test_loop_recovery import GOOD_CALL
from test_nullable_buffer import frames, NULL_SHAPES
from test_heartbeat_lifecycle import LiveHandler

C=wb.wb_codearts
REAL_ERROR=wb.record_error

class Raw(io.BytesIO):
    def __init__(self, rows, error=None):
        super().__init__(b''.join(rows)); self.error=error
    def readline(self,*args):
        data=super().readline(*args)
        if not data and self.error: raise self.error
        return data

class V1100Tests(IntegrationTests):
    def source(self, rows, error=None):
        pool=mock.Mock(); pool.config={'max_concurrent':3,'stream_timeout_seconds':10}
        account=C.CodeArtsAccount({'user_id':'same-uid'},pool)
        native=C.CodeArtsStream(Raw(rows,error),MODEL,account,pool)
        return wb._OwnedCodeArtsStream(native),account,pool

    def invoke(self, api, source, account, handler=None, body=None):
        h=handler or Handler();h.key_entry={'id':'synthetic'}
        args=(source,MODEL)
        if api.startswith('responses'):args+=(set(),{})
        args+=({'hash':'synthetic'},account,time.time())
        method={'chat':'_chat_stream_response','chat_json':'_chat_nonstream_response',
                'responses':'_responses_stream_response','responses_json':'_responses_nonstream_response',
                'messages':'_messages_stream_response','messages_json':'_messages_nonstream_response'}[api]
        with source:
            getattr(h,method)(*args,base_body=body or dict(BODY),realm='codearts',effort=MODEL)
        return h,json.dumps(h.json_body) if h.json_body is not None else b''.join(h.written).decode()

    def test_six_paths_tool_usage_and_one_release(self):
        for api in APIS:
            self.records.clear();s,a,p=self.source(turn(GOOD_CALL,'tool_calls',26))
            _,out=self.invoke(api,s,a)
            self.assertIn('lookup',out);p.release.assert_called_once_with(a)
            self.assertEqual(len(self.records),1)
            kind,args,kw=self.records[0];self.assertEqual(kind,'usage')
            self.assertEqual(args[1]['total_tokens'],26);self.assertEqual(kw['realm'],'codearts')
            self.assertNotIn('effort',kw)

    def test_six_paths_failure_keeps_received_usage(self):
        rows=[chunk({'content':'visible answer'}),b'data: {"usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":14}}\n\n']
        for api in APIS:
            self.records.clear();s,a,p=self.source(rows,ConnectionError('synthetic read failure'))
            with mock.patch.object(wb,'open_upstream') as opened:
                _,out=self.invoke(api,s,a)
            opened.assert_not_called()
            self.assertIn('error' if api.endswith('_json') else 'visible answer',out,api)
            p.release.assert_called_once_with(a);self.assertEqual(len(self.records),1)
            kind,args,kw=self.records[0];self.assertEqual(kind,'error')
            self.assertEqual(kw['usage']['total_tokens'],14);self.assertEqual(kw['realm'],'codearts')

    def test_reasoning_loop_never_reopens_workbuddy(self):
        with mock.patch.dict(os.environ,{'WB_OPS_LOOP_RETRY':'1'}):
            for api in APIS:
                s,a,p=self.source(frames(NULL_SHAPES[0],128))
                with mock.patch.object(wb,'open_upstream') as opened:
                    _,out=self.invoke(api,s,a)
                opened.assert_not_called();self.assertIn('reasoning_loop_detected',out)
                p.release.assert_called_once_with(a)

    def test_stream_cancel_releases_and_keeps_realm(self):
        for api in ('chat','responses','messages'):
            self.records.clear();s,a,p=self.source(turn({'content':'answer'}))
            self.invoke(api,s,a,Handler(writer_error=BrokenPipeError('client closed')))
            p.release.assert_called_once_with(a);self.assertEqual(len(self.records),1)
            self.assertEqual(self.records[0][2]['realm'],'codearts')
            self.assertEqual(self.records[0][2]['outcome'],'client_aborted')

    def test_concurrent_close_cannot_release_other_lease(self):
        s,a,p=self.source(turn({'content':'answer'}))
        threads=[threading.Thread(target=s.close) for _ in range(12)]
        for t in threads:t.start()
        for t in threads:t.join(1);self.assertFalse(t.is_alive())
        s._ops_codearts_source.close();p.release.assert_called_once_with(a)

    def test_same_uid_never_debits_workbuddy_credit(self):
        pool=mock.Mock();rows=[]
        with mock.patch.object(wb,'POOL',pool),mock.patch.object(wb,'_persist_usage',side_effect=lambda row,*_:rows.append(row)):
            REAL_RECORD(MODEL,{'prompt_tokens':1,'credit':3},account='same-uid',realm='codearts')
            REAL_ERROR(MODEL,502,'failed',usage={'prompt_tokens':1,'credit':3},account='same-uid',realm='codearts')
        pool.get.assert_not_called();self.assertEqual([r['realm']for r in rows],['codearts']*2)
        self.assertEqual(rows[0]['cost_policy'],'codearts:unpriced')

    def test_compaction_refuses_partial_codearts_summary(self):
        for stream in (False,True):
            self.records.clear();s,a,p=self.source(turn({'content':'partial summary'},'length'))
            h=Handler()
            with s:wb.Handler._responses_compaction_response(h,s,MODEL,{},a,time.time(),stream,realm='codearts')
            self.assertIn('error',h.json_body);self.assertNotIn('encrypted_content',json.dumps(h.json_body))
            self.assertEqual(self.records[0][2]['realm'],'codearts');p.release.assert_called_once_with(a)

    def test_responses_dispatches_to_codearts_after_key_checks(self):
        h=Handler();h._request_realm=lambda:'codearts'
        h._cross_realm_error=lambda *a:None;h._banned_model_error=lambda *a:None
        h._key_model_error=lambda *a:None;h._handle_codearts_responses=mock.Mock(return_value='routed')
        with mock.patch.object(wb,'open_upstream_with_fallback') as opened:
            self.assertEqual(wb.Handler._handle_responses(h,{'model':MODEL,'input':'test'}),'routed')
        opened.assert_not_called();h._handle_codearts_responses.assert_called_once()
        h._key_model_error=lambda *a:'denied';h._handle_codearts_responses.reset_mock()
        wb.Handler._handle_responses(h,{'model':MODEL,'input':'test'});h._handle_codearts_responses.assert_not_called()

    def test_open_preserves_key_account_allowlist(self):
        s,a,p=self.source(turn({'content':'answer'}));native=s._ops_codearts_source
        h=Handler();h._key_allowed_accounts=lambda:{'allowed'}
        with mock.patch.object(C,'load_config',return_value={}),mock.patch.object(C,'configured_config',return_value=True),mock.patch.object(C,'build_chat_body',return_value=BODY),mock.patch.object(C,'open_chat',return_value=(native,a,MODEL)) as opened:
            out,_,_=wb.Handler._open_codearts_chat(h,BODY,'session')
        self.assertEqual(opened.call_args.kwargs['allowed_accounts'],{'allowed'});out.close()

    def test_preflight_failure_only_open_owner_releases(self):
        for concurrent in (False,True):
            pool=mock.Mock();pool.config={'max_concurrent':3,'stream_timeout_seconds':10}
            a=C.CodeArtsAccount({'user_id':'a'},pool);pool.accounts=[a]
            pool.pick.side_effect=[a,None];pool.acquire.return_value=True
            msg='concurrent session' if concurrent else 'synthetic rejection'
            responses=[]
            def response(*args,**kwargs):
                r=Raw([('data: '+json.dumps({'error_code':'bad','message':msg})+'\n\n').encode()]);responses.append(r);return r
            with ExitStack() as stack:
                for name in ('_ensure_token','ensure_models'):stack.enter_context(mock.patch.object(C,name))
                stack.enter_context(mock.patch.object(C,'pool_for',return_value=pool))
                stack.enter_context(mock.patch.object(C,'_canonical_model',return_value=MODEL))
                stack.enter_context(mock.patch.object(C,'_chat_headers',return_value={}))
                stack.enter_context(mock.patch.object(C,'_signed_headers',return_value={}))
                stack.enter_context(mock.patch.object(C,'_opener',return_value=SimpleNamespace(open=response)))
                stack.enter_context(mock.patch.object(C.time,'sleep'))
                with self.assertRaises(C.CodeArtsUpstreamError):C.open_chat({'enabled':True,'accounts_dir':'unused'},BODY)
            pool.release.assert_called_once_with(a)
            self.assertEqual(len(responses),3 if concurrent else 1);self.assertTrue(all(r.closed for r in responses))

    def test_done_option_stops_underlying_guard_and_keeps_usage(self):
        class Source:
            reached=False
            def __iter__(self):
                yield from turn({'content':'answer'})
                self.reached=True
            def close(self):pass
        for enabled in (False,True):
            s=Source();holder={'model':MODEL}
            with mock.patch.dict(os.environ,{'WB_STOP_AT_SSE_DONE':'1' if enabled else '0'}):
                guarded=wb._GuardedStream(s,holder)
                try:list(guarded)
                finally:guarded.close()
            self.assertEqual(s.reached,not enabled)
            self.assertEqual(wb._saved_attempt_usage(holder)['total_tokens'],14)

    def test_error_cache_preserves_first_read_limit(self):
        for enabled in (False,True):
            e=urllib.error.HTTPError('https://example.invalid',502,'synthetic',{},io.BytesIO(b'abcdef'))
            with mock.patch.dict(os.environ,{'WB_CACHE_HTTP_ERROR_BODY':'1' if enabled else '0'}):
                self.assertEqual(wb.read_http_error(e,3),b'abc')
                self.assertEqual(wb.read_http_error(e,6),b'abc' if enabled else b'def')
            e.close()

    def test_real_socket_cancel_and_deadline_close_all_three_protocols(self):
        for cancel in (True,False):
            for api in ('chat','responses','messages'):
                with self.subTest(cancel=cancel,api=api),ExitStack() as stack:
                    self.records.clear()
                    faulthandler.dump_traceback_later(4,exit=True)
                    stack.callback(faulthandler.cancel_dump_traceback_later)
                    client,peer=socket.socketpair()
                    stack.callback(client.close);stack.callback(peer.close)
                    client.settimeout(1)
                    peer.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n'+chunk({'role':'assistant'},usage={'total_tokens':29}))
                    raw=http.client.HTTPResponse(client);raw.begin();stack.callback(raw.close)
                    pool=mock.Mock();pool.config={'max_concurrent':3}
                    a=C.CodeArtsAccount({'user_id':'a'},pool)
                    s=wb._OwnedCodeArtsStream(C.CodeArtsStream(raw,MODEL,a,pool))
                    stack.enter_context(mock.patch.dict(os.environ,{'WB_OPS_GENERATION_TIMEOUT':'.15'}))
                    stack.enter_context(mock.patch.object(wb,'SSE_HEARTBEAT_SECONDS',.02))
                    created=[];original=wb.SSEHeartbeat
                    def factory(*args,**kwargs):
                        hb=original(*args,**kwargs);created.append(hb);return hb
                    stack.enter_context(mock.patch.object(wb,'SSEHeartbeat',side_effect=factory))
                    h=LiveHandler(api,fail_heartbeat=cancel)
                    start=time.monotonic();_,out=self.invoke(api,s,a,h)
                    self.assertLess(time.monotonic()-start,.8)
                    self.assertTrue(raw.isclosed());pool.release.assert_called_once_with(a)
                    self.assertTrue(h.beat.is_set())
                    self.assertEqual(len(created),1);self.assertFalse(created[0]._thread.is_alive())
                    self.assertEqual(len(self.records),1)
                    args=self.records[0][1];kw=self.records[0][2]
                    usage=args[1] if self.records[0][0]=='usage' else kw['usage']
                    self.assertEqual(usage['total_tokens'],29)
                    self.assertEqual(kw['realm'],'codearts')
                    if cancel:self.assertEqual(kw['outcome'],'client_aborted')
                    else:self.assertIn('generation_timeout',out)

    def test_codearts_web_calls_are_not_executed_by_workbuddy(self):
        call={'tool_calls':[{'index':0,'id':'c','type':'function','function':{'name':'web_search','arguments':'{"query":"test"}'}}]}
        for api in ('responses','responses_json'):
            s,a,p=self.source(turn(call,'tool_calls'))
            with mock.patch.object(wb,'web_tools_active',return_value=True),mock.patch.object(wb,'follow_up_with_tool_results') as follow:
                _,out=self.invoke(api,s,a)
            follow.assert_not_called();self.assertIn('web_search',out);p.release.assert_called_once_with(a)

if __name__=='__main__':
    tests=unittest.TestSuite(V1100Tests(n)for n in V1100Tests.__dict__ if n.startswith('test_'))
    sys.exit(not unittest.TextTestRunner(verbosity=2).run(tests).wasSuccessful())

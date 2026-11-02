# Synthetic cases adapted from the supplied v1.10.1 / 20261102-r2 reference.
"""New rate-limit, tool validation and provider identity integration boundaries."""
import copy
import http.client
import io
import json
import os
import sys
import threading
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock
from test_v190 import V190Tests
from test_v1100 import C, Raw
from test_integration import wb, MODEL, BODY, turn


class V1101Tests(V190Tests):
    def test_invalid_tool_names_are_dropped_without_renaming(self):
        for name in (' padded', 'padded ', 'trailing\n', 123, True, '', 'x'*65, 'a.b'):
            with self.subTest(name=name):
                obj={'tools':[{'type':'function','function':{'name':name,'parameters':{'type':'object'}}}]}
                wb.normalize_tools(obj)
                self.assertNotIn('tools',obj)

    def test_legal_tools_parameters_and_input_remain_unchanged(self):
        tool={'type':'function','function':{'name':'a-B_9','strict':True,
              'parameters':{'type':'object','properties':{'q':{'type':'string'}},'required':['q']}}}
        original=copy.deepcopy(tool)
        obj={'tools':[tool,copy.deepcopy(tool),{'type':'web_search'}]}
        wb.normalize_tools(obj)
        self.assertEqual(obj['tools'],[original]);self.assertEqual(tool,original)

    def test_free_whitelisted_cooldown_instant_uses_eligible_window(self):
        with ExitStack() as stack:
            _,opened,_,_=self.context(stack)
            pool=wb.POOL
            a=self.cold_free_account();other=self.cold_free_account('outside');other.arm_model_cooldown(MODEL,time.time()+3600)
            pool.accounts=[a,other];pool.count_ready.return_value=0;pool.pick_for_session.side_effect=lambda **kw:None
            stack.enter_context(mock.patch.object(wb,'realm_model_throttled',return_value=(False,0)))
            before=time.time()
            with self.assertRaises(wb.RateLimited) as caught:
                wb.open_upstream(BODY,allowed_accounts={a.uid},deadline_at=time.monotonic()+1)
            exc=caught.exception;epoch=wb.rate_limit_reset_at(exc)
            self.assertGreater(epoch,before);self.assertLess(epoch,before+150)
            self.assertLessEqual(abs(epoch-(time.time()+exc.wait)),2)
            opened.assert_not_called();self.assertEqual(wb.account_inflight_snapshot(),{})
            for call in pool.pick_for_session.call_args_list:self.assertEqual(call.kwargs['allowed'],{a.uid})

    def test_real_keepalive_does_not_leak_rate_headers_or_body(self):
        # Exercise real HTTP framing and handle_one_request(), not only _json stubs.
        epoch=int(time.time())+120
        class ServerHandler(wb.Handler):
            def log_message(self,*a):pass
            def do_GET(self):
                if self.path=='/limited':self._error(429,'limited','rate_limit_error',reset_at=epoch)
                elif self.path=='/messages':self._anthropic_error(429,'limited','rate_limit_error',reset_at=epoch)
                else:self._json(200,{'ok':True})
        from http.server import ThreadingHTTPServer
        server=ThreadingHTTPServer(('127.0.0.1',0),ServerHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        try:
            for path in ('/limited','/messages'):
                conn.request('GET',path);response=conn.getresponse();body=json.loads(response.read())
                self.assertEqual(response.status,429)
                self.assertEqual(response.getheader('X-RateLimit-Reset'),str(epoch))
                self.assertIn('resets at',body['error']['message'])
                if path=='/limited':self.assertEqual(body['error']['reset_at'],epoch)
                sock=conn.sock;self.assertIsNotNone(sock)
                conn.request('GET','/ok');response=conn.getresponse();body=json.loads(response.read())
                self.assertEqual(response.status,200);self.assertEqual(body,{'ok':True})
                self.assertIsNone(response.getheader('X-RateLimit-Reset'))
                self.assertIsNone(response.getheader('X-RateLimit-Reset-At'))
                self.assertIs(conn.sock,sock)
        finally:
            conn.close();server.shutdown();server.server_close();thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_bad_reset_values_never_replace_rate_limit_with_exception(self):
        for value in (10**400,float('inf'),float('nan'),-1,'invalid'):
            with self.subTest(value=str(value)[:30]):
                exc=wb.RateLimited(None,'unknown window',wait=7,reset_at=value)
                self.assertIsNone(wb.rate_limit_reset_at(exc))
                self.assertEqual(wb.rate_limit_headers(value),[])
                self.assertEqual(exc.wait,7)

    def codearts_request(self, account, enabled=None, session='first'):
        requests=[];sign_inputs=[]
        def opener(request,**kw):
            requests.append(request)
            return Raw(turn({'content':'ok'}))
        def signed(account,method,url,headers,data):
            sign_inputs.append(dict(headers));return dict(headers)
        with ExitStack() as stack:
            if enabled is not None:stack.enter_context(mock.patch.dict(os.environ,{'WB_CODEARTS_OT_SESSION':enabled}))
            else:
                stack.enter_context(mock.patch.dict(os.environ));os.environ.pop('WB_CODEARTS_OT_SESSION',None)
            stack.enter_context(mock.patch.object(C,'_ensure_token'))
            stack.enter_context(mock.patch.object(C,'_chat_headers',return_value={}))
            stack.enter_context(mock.patch.object(C,'_signed_headers',side_effect=signed))
            stack.enter_context(mock.patch.object(C,'_opener',return_value=SimpleNamespace(open=opener)))
            stream,_=C._client_chat(account,dict(BODY),session)
            with wb._OwnedCodeArtsStream(stream) as owned:list(owned)
            owned.close()
        return {k.lower():v for k,v in requests[0].header_items()},sign_inputs

    def test_codearts_identity_is_account_stable_but_trace_is_request_specific(self):
        pool=mock.Mock();pool.config={'max_concurrent':3,'stream_timeout_seconds':2}
        a=C.CodeArtsAccount({'user_id':'alpha'},pool)
        first,signs=self.codearts_request(a)
        a.access_token='renewed'
        second,_=self.codearts_request(a,session='different')
        fresh=C.CodeArtsAccount({'user_id':'alpha'},pool)
        third,_=self.codearts_request(fresh)
        other,_=self.codearts_request(C.CodeArtsAccount({'user_id':'beta'},pool))
        self.assertEqual(first['x-ot-session-id'],second['x-ot-session-id'])
        self.assertEqual(first['x-ot-session-id'],third['x-ot-session-id'])
        self.assertEqual(first['x-ot-session-id'],first['user-session-id'])
        self.assertNotEqual(first['x-ot-session-id'],other['x-ot-session-id'])
        self.assertNotEqual(first['x-ot-trace-id'],second['x-ot-trace-id'])
        self.assertNotIn('x-ot-session-id',{k.lower()for k in signs[0]})
        self.assertEqual(pool.release.call_count,4)

    def test_codearts_identity_can_be_disabled_without_changing_close_ownership(self):
        pool=mock.Mock();pool.config={'max_concurrent':3,'stream_timeout_seconds':2}
        a=C.CodeArtsAccount({'user_id':'alpha'},pool)
        headers,_=self.codearts_request(a,'0')
        for key in ('x-ot-session-id','user-session-id','x-ot-trace-id'):self.assertNotIn(key,headers)
        self.assertIn('chat-id',headers);self.assertIn('session-id',headers)
        pool.release.assert_called_once_with(a)

    def test_proxy_normalization_preserves_standard_auth_and_ipv6(self):
        normal=wb.wb_settings.normalize_proxy_url
        self.assertEqual(normal('proxy.invalid:8080:alice:secret'),'http://alice:secret@proxy.invalid:8080')
        for url in ('http://alice:p%40ss@proxy.invalid:8080','socks5://alice:secret@proxy.invalid:1080','http://[::1]:8080'):
            self.assertEqual(normal(url),url)
        self.assertEqual(normal('alice:secret@proxy.invalid:8080'),'http://alice:secret@proxy.invalid:8080')
        self.assertEqual(normal(''),'')


if __name__=='__main__':
    suite=unittest.TestSuite(V1101Tests(n)for n in V1101Tests.__dict__ if n.startswith('test_'))
    sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

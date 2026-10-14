"""v1.7.2 migration and revalidated protocol boundaries, synthetic only."""
import json
import socket
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest import mock
from test_v1617 import MigrationTests
from test_integration import wb, Stream, Account, MODEL, BODY, chunk, turn

APIS = ('chat','chat_json','responses','responses_json','messages','messages_json')
DSML = '<｜DSML｜ calls><｜DSML｜ invoke name="inspect"><｜DSML｜ parameter name="path" string="true">/synthetic</｜DSML｜ parameter></｜DSML｜ invoke></｜DSML｜ calls>'
DSML = DSML.replace('｜DSML｜', '｜｜DSML｜｜')


class V172Tests(MigrationTests):
    def test_refusal_survives_and_never_retries_six_paths(self):
        for api in APIS:
            with self.subTest(api=api), mock.patch.object(wb, 'open_upstream') as retry:
                _, out = self.invoke_new(api, turn({'reasoning_content':'Consider request.', 'refusal':'Cannot assist with that.'}))
                self.assertIn('Cannot assist with that.', out)
                self.assertNotIn('reasoning_only_turn',out)
                retry.assert_not_called()

    def test_refusal_with_body_is_not_dropped(self):
        for api in APIS:
            with self.subTest(api=api):
                _,out=self.invoke_new(api,turn({'content':'Alternative.', 'refusal':'Declined.'}))
                self.assertIn('Alternative.',out);self.assertIn('Declined.',out)

    def test_multiple_upstream_candidates_fail_without_merge_or_retry(self):
        for choices in ([{'index':0,'delta':{'content':'FIRST'}},{'index':1,'delta':{'content':'SECOND'}}],
                        [{'index':1,'delta':{'content':'SECOND'}}]):
            raw=[('data: '+json.dumps({'choices':choices})+'\n\n').encode(), *turn({'content':'end'})]
            for api in APIS:
                with self.subTest(api=api,choices=len(choices)), mock.patch.object(wb,'open_upstream') as retry:
                    _,out=self.invoke_new(api,raw)
                    self.assertIn('upstream_choices_unsupported',out)
                    self.assertNotIn('SECOND',out);retry.assert_not_called()

    def test_dsml_stream_and_nonstream_recover_once(self):
        for api in ('chat','chat_json'):
            for size in (1,17,len(DSML)):
                raw=[chunk({'content':DSML[i:i+size]}) for i in range(0,len(DSML),size)]+turn({})
                with self.subTest(api=api,size=size),mock.patch.object(wb,'open_upstream') as retry:
                    h,out=self.invoke_new(api,raw)
                    self.assertIn('inspect',out);self.assertNotIn('DSML',out)
                    self.assertIn('tool_calls',out);retry.assert_not_called()

    def test_truncated_dsml_never_synthesizes_a_tool(self):
        for api in ('chat','chat_json'):
            h,out=self.invoke_new(api,[chunk({'content':DSML})])
            self.assertIn('upstream_truncated',out)
            if h.json_body:
                self.assertNotIn('tool_calls',h.json_body.get('choices',[{}])[0].get('message',{}))
            else:self.assertNotIn('"name": "inspect"',out)

    def test_length_preserves_complete_call_but_not_incomplete_parameters(self):
        for api in ('chat','chat_json'):
            h,out=self.invoke_new(api,turn({'content':DSML},finish='length'))
            if h.json_body:self.assertEqual(h.json_body['choices'][0]['message']['tool_calls'][0]['function']['name'],'inspect')
            else:self.assertIn('"name": "inspect"',out)
            h,out=self.invoke_new(api,turn({'content':'<run_code><parameter name="code">print(1)'},finish='length'))
            if h.json_body:self.assertNotIn('tool_calls',h.json_body['choices'][0]['message'])
            else:
                calls=[json.loads(line[6:]) for line in out.splitlines() if line.startswith('data: {')]
                self.assertFalse(any(c.get('delta',{}).get('tool_calls') for frame in calls for c in frame.get('choices',[])))

    def test_native_tools_take_precedence_over_printed_copy(self):
        for api in ('chat','chat_json'):
            h,out=self.invoke_new(api,turn({'content':DSML,'tool_calls':[{'index':0,'id':'native','type':'function','function':{'name':'native_inspect','arguments':'{}'}}]},finish='tool_calls'))
            self.assertIn('native_inspect',out)
            self.assertNotIn('"name": "inspect"',out)

    def test_messages_keeps_native_thinking_and_text(self):
        for api in ('messages','messages_json'):
            _,out=self.invoke_new(api,turn({'reasoning_content':'Reasoning example.', 'content':'Answer.'}))
            self.assertIn('Reasoning example.',out);self.assertIn('thinking',out);self.assertIn('Answer.',out)

    def test_empty_finish_is_not_a_terminal(self):
        raw=[chunk({'content':'One.'},finish=''),*turn({'content':'Two.'})]
        for api in APIS:
            _,out=self.invoke_new(api,raw)
            self.assertIn('One.',out);self.assertIn('Two.',out);self.assertNotIn('upstream_truncated',out)

    def test_responses_length_uses_incomplete_terminal(self):
        _,out=self.invoke_new('responses',turn({'content':'Partial.'},finish='length'))
        self.assertIn('event: response.incomplete',out)
        self.assertNotIn('event: response.completed',out)

    def test_refresh_deadline_covers_headers_tls_and_connect(self):
        wa=__import__('wb_accounts')
        class Peer(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                try:
                    self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                    for _ in range(25):
                        self.wfile.write(b'x');self.wfile.flush();time.sleep(.03)
                    self.wfile.write(b'\r\nContent-Length: 2\r\n\r\n{}')
                except OSError: pass
            def do_CONNECT(self):
                try:
                    for b in b'HTTP/1.1 200 Connection established\r\n\r\n':
                        self.wfile.write(bytes([b]));self.wfile.flush();time.sleep(.03)
                except OSError: pass
            def handle(self):
                if self.server.tls_stall:
                    self.connection.settimeout(1)
                    try:self.connection.recv(1);time.sleep(.4)
                    except OSError:pass
                else:super().handle()
        server=ThreadingHTTPServer(('127.0.0.1',0),Peer);server.daemon_threads=True;server.tls_stall=False
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        addr='127.0.0.1:'+str(server.server_port)
        try:
            for url,proxy,tls in [('http://'+addr+'/', '',False),
                                  ('https://refresh.invalid/', 'http://'+addr,False),
                                  ('https://'+addr+'/', '',True)]:
                server.tls_stall=tls
                with self.subTest(url=url,proxy=bool(proxy)),mock.patch.object(wa.wb_upstream_pool,'enabled',return_value=True):
                    started=time.monotonic()
                    with self.assertRaises((TimeoutError,OSError,urllib.error.URLError)):
                        wa.http_json(url,proxy=proxy,retries=1,deadline_at=started+.15)
                    self.assertLess(time.monotonic()-started,.55)
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_refresh_redirect_does_not_replay_and_late_dns_does_not_connect(self):
        wa=__import__('wb_accounts')
        class Redirect(BaseHTTPRequestHandler):
            calls=[]
            def log_message(self,*args):pass
            def do_GET(self):
                self.calls.append(self.path);self.send_response(302)
                self.send_header('Location','/replayed');self.send_header('Content-Length','0');self.end_headers()
        server=ThreadingHTTPServer(('127.0.0.1',0),Redirect)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError):
                wa.http_json('http://127.0.0.1:'+str(server.server_port)+'/',retries=1,deadline_at=time.monotonic()+1)
            self.assertEqual(Redirect.calls,['/'])
        finally:server.shutdown();server.server_close();thread.join(2)
        owner=wa._RefreshDeadline(time.monotonic()+.025)
        def late(*args):
            time.sleep(.04)
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',9))]
        with mock.patch.object(wa.socket,'getaddrinfo',side_effect=late),mock.patch.object(wa.socket,'socket') as connect:
            with self.assertRaises(TimeoutError):owner.connect(('late.invalid',443))
            connect.assert_not_called()

    def test_invalid_candidate_request_rejected_before_generation(self):
        for path in ('/v1/chat/completions','/v1/responses','/v1/messages'):
            for n in (2,0,-1,True,'1',1.0,None):
                with self.subTest(path=path,n=n):
                    h=wb.Handler.__new__(wb.Handler);h.path=path
                    h._is_panel_route=mock.Mock(return_value=False)
                    h._authorized=mock.Mock(return_value=True);h._key_ok=mock.Mock(return_value=True)
                    h._payload_or_error=mock.Mock(return_value={'model':MODEL,'n':n})
                    h._error=mock.Mock();h._anthropic_error=mock.Mock()
                    h._dispatch_chat_post=mock.Mock();h._handle_messages=mock.Mock()
                    h.do_POST()
                    error=h._anthropic_error if path.endswith('messages') else h._error
                    self.assertEqual(error.call_args.args[0],400)
                    h._dispatch_chat_post.assert_not_called();h._handle_messages.assert_not_called()


if __name__=='__main__':
    suite=unittest.TestSuite(V172Tests(n) for n in V172Tests.__dict__ if n.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())

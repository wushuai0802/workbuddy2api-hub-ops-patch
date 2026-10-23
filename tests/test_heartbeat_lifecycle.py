"""Heartbeats cross initial/body silence without replay, false completion or leaks."""
from contextlib import ExitStack
import http.client
from http.server import ThreadingHTTPServer
import json
import threading
import time
import types
import unittest
from unittest import mock

from test_integration import Account, BODY, Handler, IntegrationTests, MODEL, chunk, turn, wb
from test_guarded_sse_batching import ChunkSource


class PausedSource:
    def __init__(self, prefix=False, fail=False):
        self.prefix, self.fail = prefix, fail
        self.reads = self.phase = 0
        self.closed = self.interrupted = False
        self.blocked, self.release = threading.Event(), threading.Event()
        source = self
        class Socket:
            timeout = 30
            def gettimeout(self): return self.timeout
            def settimeout(self, value): self.timeout = value
            def shutdown(self, _how):
                source.interrupted = True
                source.release.set()
        self.fp = types.SimpleNamespace(raw=types.SimpleNamespace(_sock=Socket()))

    def read1(self, size):
        self.reads += 1
        if self.phase == 0 and self.prefix:
            self.phase = 1
            return chunk({'content': 'HEAD '}, usage={'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13})
        if self.phase <= 1:
            self.phase = 2
            self.blocked.set()
            if not self.release.wait(3):
                raise AssertionError('synthetic source not released')
            if self.interrupted:
                return b''
            if self.fail:
                raise ConnectionResetError('synthetic read failure')
            return b''.join(turn({'content': 'ANSWER'}, tokens=53))
        return b''

    def close(self):
        self.closed = True
        self.release.set()


class LiveHandler(Handler):
    def __init__(self, api, fail_heartbeat=False):
        super().__init__('/v1/' + api)
        self.beat = threading.Event()
        self.fail_heartbeat = fail_heartbeat

    @property
    def wfile(self):
        handler = self
        class Writer:
            def write(self, data):
                if data == b': keepalive\n\n':
                    handler.beat.set()
                    if handler.fail_heartbeat:
                        raise BrokenPipeError('synthetic downstream close')
                handler.written.append(data)
            def flush(self): pass
        return Writer()


class HeartbeatLifecycleTests(IntegrationTests):
    def context(self, stack):
        beats = []
        real = wb.SSEHeartbeat
        def create(*args, **kwargs):
            beat = real(*args, **kwargs)
            beats.append(beat)
            return beat
        stack.enter_context(mock.patch.object(wb, 'SSE_HEARTBEAT_SECONDS', .05))
        stack.enter_context(mock.patch.object(wb, 'SSEHeartbeat', side_effect=create))
        stack.enter_context(mock.patch.object(wb, 'open_upstream', side_effect=AssertionError('unexpected replay')))
        return beats

    def invoke(self, api, handler, source, **kwargs):
        args = [source, MODEL]
        if api == 'responses': args += [set(), {}]
        args += [{'hash': 'synthetic'}, Account('first'), time.time()]
        getattr(handler, '_' + api + '_stream_response')(*args, base_body=BODY, **kwargs)

    def start(self, api, handler, source):
        errors = []
        def run():
            try: self.invoke(api, handler, source)
            except BaseException as exc: errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(source.release.set)
        self.addCleanup(thread.join, 4)
        return thread, errors

    def assert_stopped(self, beats):
        self.assertTrue(beats)
        self.assertTrue(all(not beat._thread.is_alive() for beat in beats))

    def test_initial_and_body_silence_all_three_protocols(self):
        for api in ('chat', 'responses', 'messages'):
            for prefix in (False, True):
                with self.subTest(api=api, prefix=prefix), ExitStack() as stack:
                    beats = self.context(stack)
                    source, handler = PausedSource(prefix=prefix), LiveHandler(api)
                    thread, errors = self.start(api, handler, source)
                    self.assertTrue(source.blocked.wait(1))
                    self.assertIn(('status', 200), handler.headers_sent)
                    self.assertTrue(handler.beat.wait(1), 'no heartbeat during a blocked read')
                    self.assertTrue(thread.is_alive())
                    source.release.set(); thread.join(2)
                    self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
                    output = b''.join(handler.written)
                    self.assertIn(b'ANSWER', output)
                    terminal = {'chat': b'[DONE]', 'responses': b'"type": "response.completed"',
                                'messages': b'event: message_stop'}[api]
                    self.assertEqual(output.count(terminal), 1)
                    self.assertTrue(source.closed)
                    self.assert_stopped(beats)

    def test_heartbeat_disconnect_aborts_owned_read_without_replay_or_completion(self):
        for api in ('chat', 'responses', 'messages'):
            with self.subTest(api=api), ExitStack() as stack:
                self.records.clear()
                beats = self.context(stack)
                source, handler = PausedSource(), LiveHandler(api, fail_heartbeat=True)
                thread, errors = self.start(api, handler, source)
                self.assertTrue(source.blocked.wait(1))
                self.assertTrue(handler.beat.wait(1))
                thread.join(2)
                self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
                self.assertTrue(source.closed and source.interrupted)
                self.assertEqual(source.reads, 1)
                self.assertFalse(any(row[2].get('outcome') == 'completed' for row in self.records))
                output = b''.join(handler.written)
                self.assertNotIn(b'ANSWER', output)
                self.assertNotIn(b'response.completed', output)
                self.assert_stopped(beats)

    def test_generation_deadline_is_not_extended_by_downstream_heartbeats(self):
        for api in ('chat', 'responses', 'messages'):
            with self.subTest(api=api), ExitStack() as stack:
                beats = self.context(stack)
                source, handler = PausedSource(prefix=True), LiveHandler(api)
                source._ops_generation_deadline = time.monotonic() + .2
                started = time.monotonic()
                thread, errors = self.start(api, handler, source)
                self.assertTrue(handler.beat.wait(1))
                thread.join(2)
                self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
                self.assertLess(time.monotonic() - started, 1)
                self.assertIn(b'generation_timeout', b''.join(handler.written))
                self.assertTrue(source.closed and source.interrupted)
                self.assert_stopped(beats)

    def test_read_failure_keeps_error_terminal_and_consumed_usage(self):
        for api in ('chat', 'responses', 'messages'):
            with self.subTest(api=api), ExitStack() as stack:
                self.records.clear()
                beats = self.context(stack)
                source, handler = PausedSource(prefix=True, fail=True), LiveHandler(api)
                thread, errors = self.start(api, handler, source)
                self.assertTrue(handler.beat.wait(1))
                source.release.set(); thread.join(2)
                self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
                output = b''.join(handler.written)
                self.assertIn(b'HEAD', output)
                self.assertNotIn(b'response.completed', output)
                failed = [row for row in self.records if row[0] == 'error']
                self.assertEqual(len(failed), 1)
                self.assertEqual(failed[0][2]['usage']['total_tokens'], 13)
                self.assertTrue(source.closed)
                self.assert_stopped(beats)

    def test_chat_reopen_stops_previous_heartbeat_before_next_stream(self):
        with ExitStack() as stack:
            beats = self.context(stack)
            first, handler = PausedSource(fail=True), LiveHandler('chat')
            second = ChunkSource([b''.join(turn({'content': 'REOPENED'}))])
            def reopen():
                self.assertFalse(beats[0]._thread.is_alive())
                return second, Account('second'), 'high'
            # The dedicated probe contracts are covered separately. This case
            # starts after a successful first-frame probe but no business output.
            with mock.patch.object(wb, 'probe_first_frame', side_effect=lambda source, **kwargs: (source, None)):
                errors = []
                def run():
                    try: self.invoke('chat', handler, first, reopen=reopen)
                    except BaseException as exc: errors.append(exc)
                thread = threading.Thread(target=run); thread.start()
                self.assertTrue(handler.beat.wait(1))
                first.release.set(); thread.join(2)
                self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
            self.assertIn(b'REOPENED', b''.join(handler.written))
            self.assertEqual(len(beats), 2)
            self.assert_stopped(beats)

    def test_native_heartbeat_surfaces_background_write_error(self):
        def broken(_data): raise BrokenPipeError('synthetic heartbeat failure')
        beat = wb.SSEHeartbeat(broken, interval=.05)
        try:
            beat._thread.join(1)
            self.assertFalse(beat._thread.is_alive())
            with self.assertRaises(BrokenPipeError): beat.write(b'data: must not disappear\n\n')
        finally: beat.close()

    def test_real_http_initial_silence_sends_comment_before_first_data(self):
        for api in ('chat', 'responses', 'messages'):
            with self.subTest(api=api), ExitStack() as stack:
                beats = self.context(stack)
                source = PausedSource()
                case = self
                class LocalHandler(wb.Handler):
                    def log_message(self, *args): pass
                    def do_POST(self):
                        self.rfile.read(int(self.headers.get('Content-Length', 0)))
                        case.invoke(api, self, source)
                server = ThreadingHTTPServer(('127.0.0.1', 0), LocalHandler)
                server.daemon_threads = True
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
                thread.start()
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                try:
                    connection.request('POST', '/v1/' + api, body=b'{}')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    prefix = bytearray()
                    while True:
                        line = response.readline()
                        self.assertTrue(line, 'stream ended before its heartbeat')
                        prefix.extend(line)
                        if line.startswith(b': keepalive'):
                            break
                    self.assertTrue(source.blocked.is_set())
                    self.assertNotIn(b'ANSWER', prefix)
                    source.release.set()
                    output = bytes(prefix) + response.read()
                    self.assertIn(b'ANSWER', output)
                    for line in output.splitlines():
                        if line.startswith(b'data: ') and line != b'data: [DONE]':
                            self.assertIsInstance(json.loads(line[6:]), dict)
                finally:
                    source.release.set()
                    connection.close()
                    server.shutdown(); server.server_close(); thread.join(2)
                self.assert_stopped(beats)


if __name__ == '__main__':
    suite = unittest.TestSuite(HeartbeatLifecycleTests(name) for name in HeartbeatLifecycleTests.__dict__ if name.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

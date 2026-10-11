"""Real loopback HTTP/1.1 connections across probe, guard and pool ownership.

Run through scripts/test_offline.py, or provide WB_TEST_SOURCE (the patched
v1.6.20 source) and WB_OPS_TEST_STATE. The server never leaves 127.0.0.1.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
import time
import unittest
from unittest import mock
import urllib.request

from test_integration import Account, BODY, Handler, MODEL, chunk, responses, turn, wb


class _LoopbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # Closing an idle keep-alive socket can abort the server's next
            # request-line read, especially on Windows.
            self.close_connection = True

    def do_GET(self):
        scenario = self.server.scenarios[self.path]
        with self.server.requests_lock:
            self.server.requests.append((self.path, self.client_address[1]))
        body = scenario["body"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if scenario.get("chunked"):
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            if scenario.get("chunked"):
                for piece in (body[:7], body[7:]):
                    if piece:
                        self.wfile.write(("%x\r\n" % len(piece)).encode() + piece + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            elif "pause" in scenario:
                prefix = scenario["prefix"]
                self.wfile.write(body[:prefix])
                self.wfile.flush()
                scenario["started"].set()
                if not scenario["pause"].wait(4):
                    return
                self.wfile.write(body[prefix:])
            else:
                self.wfile.write(body)
            self.wfile.flush()
        except (OSError, ValueError):
            # Expected when a deadline/loop guard shuts down the client socket.
            self.close_connection = True


class _OpaqueFile:
    """Keep the real HTTP reader, but hide its socket ownership attributes."""
    def __init__(self, file):
        self.file = file

    def __getattr__(self, name):
        if name in ("raw", "_sock"):
            raise AttributeError(name)
        return getattr(self.file, name)


class KeepAliveGuardTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {
            "WB_UPSTREAM_KEEPALIVE": "1", "WB_OPS_LOOP_RETRY": "1",
            "WB_OPS_LOOP_GUARD": "1", "WB_OPS_REASONING_BUFFER": "1",
        }))
        self.enterContext(mock.patch.object(wb, "record_usage"))
        self.enterContext(mock.patch.object(wb, "record_error"))
        self.pool_module = wb.wb_accounts.wb_upstream_pool
        self.pool = self.pool_module._KeepAlivePool()
        self.enterContext(mock.patch.object(self.pool_module, "_POOL", self.pool))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.server.scenarios = {}
        self.server.requests = []
        self.server.requests_lock = threading.Lock()
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        for scenario in self.server.scenarios.values():
            if "pause" in scenario:
                scenario["pause"].set()
        self.pool.close_all()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def scenario(self, name, body, *, chunked=False, prefix=None):
        value = {"body": body, "chunked": chunked}
        if prefix is not None:
            value.update(prefix=prefix, pause=threading.Event(), started=threading.Event())
        self.server.scenarios[name] = value
        return value

    def open(self, name):
        request = urllib.request.Request("http://127.0.0.1:%d%s" %
                                         (self.server.server_port, name))
        response = wb.wb_accounts.urlopen(request, timeout=2, proxy="")
        self.addCleanup(response.close)
        scenario = self.server.scenarios[name]
        if "started" in scenario:
            self.assertTrue(scenario["started"].wait(2), "server did not send the prefix")
        return response

    def idle(self):
        return sum(self.pool.idle_counts().values())

    def assert_next_request_uses_new_connection(self, previous):
        self.scenario("/after", b"next request\n")
        response = self.open("/after")
        self.assertIsNot(response._pool_conn, previous)
        self.assertEqual(response.read(), b"next request\n")
        self.assertEqual(self.pool.stats["created"], 2)
        self.assertEqual(self.pool.stats["reused"], 0)

    def test_complete_length_and_chunked_eof_reuse_the_same_connection(self):
        self.scenario("/length", b"one\ntwo\n")
        self.scenario("/chunked", b"three\nfour\n", chunked=True)
        first = self.open("/length")
        self.assertEqual(b"".join(first), b"one\ntwo\n")
        self.assertTrue(first._released)
        self.assertEqual(self.idle(), 1)
        second = self.open("/chunked")
        self.assertIs(second._pool_conn, first._pool_conn)
        self.assertEqual(b"".join(second), b"three\nfour\n")
        self.assertTrue(second._released)
        self.assertEqual(self.idle(), 1)
        self.assertEqual(self.pool.stats["created"], 1)
        self.assertEqual(self.pool.stats["reused"], 1)
        self.assertEqual(self.server.requests[0][1], self.server.requests[1][1])

    def test_probe_replays_prefix_once_and_remainder_keeps_streaming(self):
        prefix = b": keepalive\n" + chunk({"content": "first"})
        body = prefix + b"".join(turn({"content": "second"}))
        scenario = self.scenario("/probe", body, prefix=len(prefix))
        raw = self.open("/probe")
        # http.client variants can hide fp.raw; the active lease still owns
        # _pool_conn.sock. Exercise that fallback through the real reader.
        raw.fp = _OpaqueFile(raw.fp)
        self.assertIs(wb._stream_socket(raw), raw._pool_conn.sock)
        probed, account = wb.probe_first_frame(raw, deadline_at=time.monotonic() + 2)
        self.assertIsNone(account)
        self.assertFalse(raw._released)
        self.assertEqual(self.idle(), 0)
        self.assertIs(wb._stream_socket(probed), raw._pool_conn.sock)
        scenario["pause"].set()
        self.assertEqual(b"".join(probed), body)
        probed.close()
        self.assertTrue(raw._released)
        self.assertEqual(self.idle(), 1)
        self.scenario("/after-probe", b"reused\n")
        later = self.open("/after-probe")
        self.assertIs(later._pool_conn, raw._pool_conn)
        self.assertEqual(later.read(), b"reused\n")
        self.assertEqual(self.pool.stats["reused"], 1)

    def test_probe_deadline_discards_connection_and_does_not_reopen(self):
        self.scenario("/probe-timeout", b": waiting\n" + chunk({"content": "too late"}),
                      prefix=len(b": waiting\n"))
        raw = self.open("/probe-timeout")
        previous = raw._pool_conn
        began = time.monotonic()
        with self.assertRaises((TimeoutError, OSError)):
            wb.probe_first_frame(raw, deadline_at=began + .12)
        self.assertLess(time.monotonic() - began, 2)
        self.assertTrue(raw._released)
        self.assertEqual(self.idle(), 0)
        self.assertEqual([name for name, _port in self.server.requests], ["/probe-timeout"])
        self.assert_next_request_uses_new_connection(previous)

    def test_generation_deadline_unblocks_read_and_discards_connection(self):
        self.scenario("/generation-timeout", b": waiting\n" + b"".join(turn({"content": "late"})),
                      prefix=len(b": waiting\n"))
        raw = self.open("/generation-timeout")
        previous = raw._pool_conn
        holder = {"model": MODEL, "base_body": BODY,
                  "generation_deadline": time.monotonic() + .12}
        guard = wb._GuardedStream(raw, holder)
        try:
            frames = b"".join(guard)
        finally:
            guard.close()
        self.assertIn(b"generation_timeout", frames)
        self.assertNotIn(b'"content": "late"', frames)
        self.assertTrue(raw._released)
        self.assertEqual(self.idle(), 0)
        self.assert_next_request_uses_new_connection(previous)

    def test_interruption_never_recycles_even_if_buffer_reaches_natural_eof(self):
        body = b"prefix\nfinal\n"
        self.scenario("/buffered", body)
        raw = self.open("/buffered")
        previous = raw._pool_conn
        self.assertEqual(raw.read(7), b"prefix\n")
        self.assertGreaterEqual(len(raw.fp.peek(6)), 6)
        wb._interrupt_upstream(raw)
        self.assertTrue(raw.will_close)
        # Buffered bytes survive socket shutdown. Reading them can naturally
        # end HTTPResponse, but the interrupted lease must still be discarded.
        self.assertEqual(raw.read(), b"final\n")
        self.assertTrue(raw._released)
        self.assertEqual(self.idle(), 0)
        self.assert_next_request_uses_new_connection(previous)

    def test_loop_after_tool_does_not_replay_or_recycle_the_socket(self):
        call = {"tool_calls": [{"index": 0, "id": "call_once",
                                "function": {"name": "lookup", "arguments": "{}"}}]}
        loop = "Let me go.\n" * 120
        body = chunk(call) + chunk({"reasoning_content": loop}) + b"".join(turn())
        self.scenario("/tool-loop", body)
        raw = self.open("/tool-loop")
        previous = raw._pool_conn
        handler = Handler("/v1/chat/completions")
        with mock.patch.object(wb, "open_upstream") as reopened:
            handler._chat_stream_response(raw, MODEL, {}, Account(), time.time(), base_body=BODY)
        reopened.assert_not_called()
        output = responses(handler)
        self.assertTrue(any((frame.get("error") or {}).get("code") == "reasoning_loop_detected"
                            for frame in output))
        calls = [tool for frame in output for choice in frame.get("choices", [])
                 for tool in (choice.get("delta") or {}).get("tool_calls", [])]
        self.assertEqual(sum(tool.get("id") == "call_once" for tool in calls), 1)
        self.assertTrue(raw._released)
        self.assertEqual(self.idle(), 0)
        self.assertEqual([name for name, _port in self.server.requests], ["/tool-loop"])
        self.assert_next_request_uses_new_connection(previous)

    def test_released_response_cannot_interrupt_a_new_lease_on_its_connection(self):
        self.scenario("/first", b"complete\n")
        first = self.open("/first")
        self.assertEqual(first.read(), b"complete\n")
        prefix = chunk({"content": "new owner"})
        body = prefix + b"".join(turn({"content": "still healthy"}))
        scenario = self.scenario("/new-owner", body, prefix=len(prefix))
        new_owner = self.open("/new-owner")
        self.assertIs(new_owner._pool_conn, first._pool_conn)
        sock = wb._stream_socket(new_owner)
        sock.settimeout(1.75)
        self.assertIsNone(wb._stream_socket(first))
        wb._interrupt_upstream(first)
        self.assertEqual(sock.gettimeout(), 1.75)
        self.assertFalse(new_owner.will_close)
        self.assertFalse(new_owner._released)
        scenario["pause"].set()
        self.assertEqual(new_owner.read(), body)
        self.assertEqual(self.idle(), 1)
        self.assertEqual(self.pool.stats["discarded"], 0)


if __name__ == "__main__":
    unittest.main()

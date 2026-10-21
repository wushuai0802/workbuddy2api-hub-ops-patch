"""Cross-version safeguards, streaming lifecycles, quotas and web decoding."""
import email.message
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent / "legacy"))
import _bootstrap

wb = _bootstrap.wb
web = wb.wb_webtools
REAL_RECORD = wb.record_usage
MODEL = "deepseek-v4.1-flash"
BODY = {"model": MODEL, "messages": [{"role": "user", "content": "test"}], "stream": True}
ROTATION = (Path(__file__).parent / "fixtures" / "pi_short_phrase_rotation.txt").read_text(encoding="utf-8")


def chunk(delta=None, finish=None, usage=None):
    frame = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        frame["usage"] = usage
    return ("data: " + json.dumps(frame) + "\n\n").encode()


def turn(delta=None, finish="stop", tokens=14):
    return [chunk(delta), chunk(finish=finish, usage={
        "prompt_tokens": tokens - 4, "completion_tokens": 4, "total_tokens": tokens}),
        b"data: [DONE]\n\n"]


class Stream:
    def __init__(self, frames, error=None):
        self.frames, self.error, self.closed = frames, error, False

    def __iter__(self):
        yield from self.frames
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


class Account:
    realm = "intl"

    def __init__(self, uid="a1"):
        self.uid = uid


class Handler:
    def _handle_expect_continue(self):
        pass

    def _discard_body(self):
        pass

    _key_id = wb.Handler._key_id
    _anthropic_error = wb.Handler._anthropic_error
    _messages_stream_response = wb.Handler._messages_stream_response
    _messages_nonstream_response = wb.Handler._messages_nonstream_response
    _responses_stream_response = wb.Handler._responses_stream_response
    _responses_nonstream_response = wb.Handler._responses_nonstream_response
    _chat_stream_response = wb.Handler._chat_stream_response
    _chat_nonstream_response = wb.Handler._chat_nonstream_response

    def __init__(self, path="/v1/responses", writer_error=None):
        self.path, self.writer_error = path, writer_error
        self.headers = {}
        self.headers_sent, self.written = [], []
        self.json_body = None

    def send_response(self, status):
        self.headers_sent.append(("status", status))

    def send_header(self, name, value):
        self.headers_sent.append((name, value))

    def end_headers(self):
        pass

    def _json(self, status, payload):
        self.json_body = payload
        return status, payload

    def _error(self, status, message, kind="server_error"):
        return self._json(status, {"error": {"message": message, "type": kind}})

    @property
    def wfile(self):
        outer = self

        class Writer:
            def write(self, data):
                if outer.writer_error:
                    raise outer.writer_error
                outer.written.append(data)

            def flush(self):
                pass

        return Writer()


def responses(handler):
    result = []
    for block in b"".join(handler.written).decode().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                result.append(json.loads(line[6:]))
    return result


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        # Keep r1/r2 fail-closed expectations, testing the supported opt-out.
        # test_loop_recovery.py explicitly enables and verifies the r3 default.
        self.enterContext(mock.patch.dict(os.environ, {"WB_OPS_LOOP_RETRY": "0"}))
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ["WB_OPS_TEST_STATE"])))
        (root / "accounts").mkdir()
        (root / "usage").mkdir()
        self.enterContext(mock.patch.multiple(wb, POOL=None, ACCOUNTS_DIR=str(root / "accounts"),
                                             USAGE_DIR=str(root / "usage"),
                                             USAGE_LOG=str(root / "usage" / "usage.jsonl")))
        self.records = []
        self.enterContext(mock.patch.object(wb, "record_usage", autospec=True, side_effect=lambda *a, **k: self.records.append(("usage", a, k))))
        self.enterContext(mock.patch.object(wb, "record_error", autospec=True, side_effect=lambda *a, **k: self.records.append(("error", a, k))))

    def response(self, stream, body=None, account=None):
        handler = Handler()
        handler._responses_stream_response(stream, MODEL, set(), {}, {"hash": "test"},
                                            account or Account(), time.time(), base_body=body)
        return handler

    def chat(self, stream, body=None, account=None):
        handler = Handler("/v1/chat/completions")
        handler._chat_stream_response(stream, MODEL, {"hash": "test"}, account or Account(),
                                      time.time(), base_body=body)
        return handler

    def assert_lifecycle(self, handler, terminal="response.completed"):
        events = responses(handler)
        self.assertEqual(sum(e["type"] == "response.created" for e in events), 1)
        terminals = [e for e in events if e["type"] in ("response.completed", "response.failed", "response.incomplete")]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["type"], terminal)
        seq = [e["sequence_number"] for e in events]
        self.assertEqual(seq, list(range(seq[0], seq[0] + len(seq))))
        ids = {e["response"]["id"] for e in events if "response" in e}
        self.assertEqual(len(ids), 1)
        added = [e for e in events if e["type"] == "response.output_item.added"]
        self.assertEqual(len({e["output_index"] for e in added}), len(added))
        self.assertEqual(len({e["item"]["id"] for e in added}), len(added))
        return terminals[0]["response"]

    def test_response_normal(self):
        stream = Stream(turn({"content": "answer"}))
        final = self.assert_lifecycle(self.response(stream))
        self.assertEqual(final["output_text"], "answer")
        self.assertTrue(stream.closed)

    def test_response_thinking_retry(self):
        first, second = Stream(turn({"reasoning_content": "thinking"})), Stream(turn({"content": "answer"}, tokens=26))
        with mock.patch.object(wb, "open_upstream", return_value=(second, Account("a2"))) as opened:
            handler = self.response(first, BODY)
        final = self.assert_lifecycle(handler)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(final["usage"]["total_tokens"], 40)
        self.assertEqual(final["output_text"], "answer")
        self.assertEqual({r[2].get("account") for r in self.records}, {"a1", "a2"})
        self.assertTrue(first.closed and second.closed)

    def test_response_empty_retry_has_only_one_message(self):
        with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn({"content": "answer"})), Account("a2"))):
            final = self.assert_lifecycle(self.response(Stream(turn()), BODY))
        self.assertEqual(sum(item["type"] == "message" for item in final["output"]), 1)

    def test_response_retry_budget(self):
        with mock.patch.object(wb, "open_upstream", side_effect=lambda *a, **k: (Stream(turn({"reasoning_content": "thinking"})), Account("a2"))) as opened:
            final = self.assert_lifecycle(self.response(Stream(turn({"reasoning_content": "thinking"})), BODY), "response.failed")
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(final["error"]["code"], "reasoning_only_turn")
        self.assertNotIn("incomplete_details", final)

    def test_response_body_prevents_retry(self):
        with mock.patch.object(wb, "open_upstream") as opened:
            self.assert_lifecycle(self.response(Stream(turn({"content": "answer", "reasoning_content": "thinking"})), BODY))
        opened.assert_not_called()

    def test_response_tool_args_only_are_preserved(self):
        frames = [chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "exec", "arguments": '{"cmd":'}}]}),
                  chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"ls"}'}}]}),
                  chunk(finish="tool_calls"), b"data: [DONE]\n\n"]
        with mock.patch.object(wb, "open_upstream") as opened:
            final = self.assert_lifecycle(self.response(Stream(frames), BODY))
        opened.assert_not_called()
        call = next(item for item in final["output"] if item["type"] == "function_call")
        self.assertEqual(json.loads(call["arguments"]), {"cmd": "ls"})

    def test_response_truncated_tool_is_not_completed(self):
        frame = chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "exec", "arguments": '{"cmd":'}}]})
        handler = self.response(Stream([frame]), BODY)
        self.assert_lifecycle(handler, "response.failed")
        self.assertFalse(any(e["type"] == "response.function_call_arguments.done" for e in responses(handler)))

    def test_response_upstream_reset_is_failure_not_client_abort(self):
        handler = self.response(Stream([chunk({"content": "partial"})], ConnectionResetError("upstream reset")), BODY)
        events = responses(handler)
        errors = [e for e in events if e.get("type") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["code"], "upstream_stream_error")
        self.assertFalse(any(e.get("type") == "response.completed" for e in events))
        self.assertEqual(self.records[-1][0], "error")

    def test_response_truncated_text_fails_without_replay(self):
        with mock.patch.object(wb, "open_upstream") as opened:
            final = self.assert_lifecycle(self.response(Stream([chunk({"content": "partial"}), b"data: [DONE]\n\n"]), BODY), "response.failed")
        opened.assert_not_called()
        self.assertEqual(final["error"]["code"], "upstream_truncated")

    def test_response_client_disconnect_closes_upstream(self):
        handler = Handler(writer_error=BrokenPipeError("client left"))
        stream = Stream(turn({"content": "answer"}))
        handler._responses_stream_response(stream, MODEL, set(), {}, {}, Account(), time.time(), base_body=BODY)
        self.assertTrue(stream.closed)
        self.assertEqual(self.records[-1][2]["outcome"], "client_aborted")

    def test_chat_normal_terminal_piece_is_not_duplicated(self):
        handler = self.chat(Stream([chunk({"content": "answer"}, "stop"), b"data: [DONE]\n\n"]))
        frames = responses(handler)
        text = "".join(c["delta"].get("content", "") for f in frames for c in f.get("choices", []))
        self.assertEqual(text, "answer")
        self.assertEqual(sum(bool(c.get("finish_reason")) for f in frames for c in f.get("choices", [])), 1)
        self.assertEqual(b"".join(handler.written).count(b"data: [DONE]"), 1)

    def test_chat_retry_identity_and_usage(self):
        with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn({"content": "answer"}, tokens=26)), Account("a2"))) as opened:
            handler = self.chat(Stream(turn({"reasoning_content": "thinking"})), BODY)
        frames = responses(handler)
        self.assertEqual(len({f["id"] for f in frames}), 1)
        self.assertEqual(len({f["created"] for f in frames}), 1)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(frames[-1]["usage"]["total_tokens"], 40)
        self.assertEqual(b"".join(handler.written).count(b"data: [DONE]"), 1)

    def test_chat_retry_budget(self):
        with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn({"reasoning_content": "thinking"})), Account("a2"))) as opened:
            handler = self.chat(Stream(turn({"reasoning_content": "thinking"})), BODY)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(next(f for f in responses(handler) if "error" in f)["error"]["code"], "reasoning_only_turn")
        self.assertEqual(b"".join(handler.written).count(b"data: [DONE]"), 1)

    def test_chat_truncated_text_is_not_replayed(self):
        with mock.patch.object(wb, "open_upstream") as opened:
            handler = self.chat(Stream([chunk({"content": "partial"}), b"data: [DONE]\n\n"]), BODY)
        opened.assert_not_called()
        self.assertTrue(any("error" in f for f in responses(handler)))
        self.assertEqual(b"".join(handler.written).count(b"data: [DONE]"), 1)

    def test_pi_reported_rotation_is_cut_in_both_protocols(self):
        phrases = ["Let me read.", "Let me run.", "Let me go.", "Let me do it.", "Let me emit.", "OK."]
        frames = [chunk({"reasoning_content": line + "\n"}) for line in phrases * 300]
        for method in (self.response, self.chat):
            stream = Stream(frames)
            handler = method(stream, BODY)
            self.assertIn(b"reasoning_loop_detected", b"".join(handler.written))
            self.assertLess(len(b"".join(handler.written)), 40000)
            self.assertTrue(stream.closed)

    def test_split_reasoning_dsml_is_not_forwarded(self):
        frames = [chunk({"reasoning_content": x}) for x in ("safe <DS", "ML calls><DSML invoke name=\"exec\">", "x</DSML invoke></DSML calls> tail")]
        frames += turn({"content": "answer"})
        for method in (self.response, self.chat):
            handler = method(Stream(frames), BODY)
            self.assertNotIn(b"DSML", b"".join(handler.written))
            self.assertNotIn(b"invoke", b"".join(handler.written))

    def test_real_rotation_both_channels_and_protocols(self):
        for channel in ("reasoning_content", "content"):
            for size in (1, 17, 128, 2048):
                for method in (self.response, self.chat):
                    with self.subTest(channel=channel, size=size, protocol=method.__name__):
                        frames = [chunk({channel: ROTATION[i:i + size]}) for i in range(0, len(ROTATION), size)]
                        frames += turn({"tool_calls": [{"index": 0, "id": "late", "function": {
                            "name": "exec", "arguments": '{"command":"must not execute"}'}}]}, "tool_calls")
                        stream = Stream(frames)
                        with mock.patch.object(wb, "open_upstream") as opened:
                            handler = method(stream, BODY)
                        opened.assert_not_called()
                        self.assertIn(("reasoning_loop_detected" if channel == "reasoning_content" else "content_loop_detected").encode(), b"".join(handler.written))
                        self.assertNotIn(b"must not execute", b"".join(handler.written))
                        self.assertTrue(stream.closed)
                        if method == self.response:
                            self.assert_lifecycle(handler, "response.failed")

    def test_large_delta_loop_cannot_hide_before_normal_suffix(self):
        body = ROTATION + "\n" + "This is a completely different and ordinary closing sentence.\n" * 70
        handler = self.chat(Stream(turn({"reasoning_content": body})), BODY)
        self.assertIn(b"reasoning_loop_detected", b"".join(handler.written))

    def test_repetition_in_tool_arguments_is_preserved(self):
        calls = {"tool_calls": [{"index": 0, "id": "tool", "function": {
            "name": "write_file", "arguments": json.dumps({"content": ROTATION * 2})}}]}
        for method in (self.response, self.chat):
            handler = method(Stream(turn(calls, "tool_calls")), BODY)
            self.assertNotIn(b"loop_detected", b"".join(handler.written))
            if method == self.response:
                final = self.assert_lifecycle(handler)
                call = next(x for x in final["output"] if x["type"] == "function_call")
                self.assertEqual(json.loads(call["arguments"])["content"], ROTATION * 2)

    def test_dsml_arguments_are_not_scanned_as_prose(self):
        body = '<DSML calls><DSML invoke name="write"><DSML parameter name="text">' + ROTATION + '</DSML parameter></DSML invoke></DSML calls>'
        for method in (self.response, self.chat):
            handler = method(Stream(turn({"content": body})), BODY)
            self.assertNotIn(b"loop_detected", b"".join(handler.written))

    def test_nonstream_loops_fail_without_tool_completion(self):
        for channel in ("reasoning_content", "content"):
            frames = [chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "exec", "arguments": '{"cmd":'}}]}),
                      chunk({channel: ROTATION}), chunk(finish="tool_calls")]
            with mock.patch.object(wb, "open_upstream") as opened:
                for protocol in ("chat", "responses"):
                    handler = Handler()
                    stream = Stream(frames)
                    if protocol == "chat":
                        handler._chat_nonstream_response(stream, MODEL, {}, Account(), time.time(), base_body=BODY)
                        self.assertIn("loop_detected", handler.json_body["error"]["message"])
                    else:
                        handler._responses_nonstream_response(stream, MODEL, set(), {}, {}, Account(), time.time(), base_body=BODY)
                        self.assertEqual(handler.json_body["status"], "failed")
                        self.assertFalse(any(x["type"] == "function_call" for x in handler.json_body["output"]))
                    self.assertTrue(stream.closed)
            opened.assert_not_called()

    def test_active_stream_obeys_total_generation_deadline(self):
        class ActiveStream(Stream):
            def __iter__(self):
                for i in range(1000):
                    yield chunk({"content": "progress %d\n" % i})
                    time.sleep(.005)
        with mock.patch.dict(os.environ, {"WB_OPS_GENERATION_TIMEOUT": "0.08"}):
            for method in (self.response, self.chat):
                stream = ActiveStream([])
                started = time.monotonic()
                with mock.patch.object(wb, "open_upstream") as opened:
                    handler = method(stream, BODY)
                self.assertLess(time.monotonic() - started, 1)
                opened.assert_not_called()
                self.assertIn(b"generation_timeout", b"".join(handler.written))
                self.assertTrue(stream.closed)
                if method == self.response:
                    self.assert_lifecycle(handler, "response.failed")

    def test_timeout_does_not_finalize_partial_tool(self):
        class PartialTool(Stream):
            def __iter__(self):
                yield chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "exec", "arguments": '{"cmd":'}}]})
                time.sleep(.08)
                yield chunk(finish="tool_calls")
        with mock.patch.dict(os.environ, {"WB_OPS_GENERATION_TIMEOUT": "0.04"}):
            handler = self.response(PartialTool([]), BODY)
        self.assert_lifecycle(handler, "response.failed")
        self.assertFalse(any(e["type"] == "response.function_call_arguments.done" for e in responses(handler)))

    def test_watchdog_interrupts_blocked_socket(self):
        left, right = socket.socketpair()
        class SocketStream:
            fp = left.makefile("rb")
            def __iter__(self):
                return iter(self.fp)
            def close(self):
                self.fp.close()
                left.close()
        holder = {}
        started = time.monotonic()
        try:
            with mock.patch.dict(os.environ, {"WB_OPS_GENERATION_TIMEOUT": "0.06"}):
                guarded = wb._GuardedStream(SocketStream(), holder)
                try:
                    frames = list(guarded)
                finally:
                    guarded.close()
            self.assertLess(time.monotonic() - started, 1)
            self.assertIn(b"generation_timeout", b"".join(frames))
            self.assertFalse(guarded.timer.is_alive())
        finally:
            right.close()
            left.close()

    def test_watchdog_interrupts_trickled_unterminated_sse_line(self):
        left, right = socket.socketpair()
        stopped = threading.Event()
        class SocketStream:
            fp = left.makefile("rb")
            def __iter__(self):
                return iter(self.fp)
            def close(self):
                self.fp.close()
                left.close()
        def trickle():
            while not stopped.wait(.005):
                try:
                    right.sendall(b" ")
                except OSError:
                    break
        worker = threading.Thread(target=trickle, daemon=True)
        worker.start()
        started = time.monotonic()
        try:
            with mock.patch.dict(os.environ, {"WB_OPS_GENERATION_TIMEOUT": "0.06"}):
                result = wb.aggregate_stream(SocketStream(), MODEL, None)
            self.assertEqual(result["_ops_failure"], "generation_timeout")
            self.assertLess(time.monotonic() - started, 1)
        finally:
            stopped.set()
            right.close()
            left.close()
            worker.join(1)

    def test_deadline_is_shared_and_cannot_restart_on_retry(self):
        holder = {"generation_deadline": time.monotonic() - 1}
        stream = Stream(turn({"content": "late"}))
        result = wb.aggregate_stream(stream, MODEL, None, holder)
        self.assertEqual(result["_ops_failure"], "generation_timeout")
        self.assertTrue(stream.closed)
        callback = mock.Mock()
        holder["retry_call"] = callback
        self.assertFalse(wb._start_empty_continuation(holder))
        callback.assert_not_called()
        self.assertEqual(holder["continuation_failure_code"], "generation_timeout")
        callback = mock.Mock(side_effect=TimeoutError("connect deadline"))
        holder = {"generation_deadline": 101, "retry_call": callback}
        with mock.patch.object(wb.time, "monotonic", side_effect=[100, 100, 102]):
            self.assertFalse(wb._start_empty_continuation(holder))
        self.assertEqual(holder["continuation_failure_code"], "generation_timeout")

    def test_normal_finish_cancels_watchdog(self):
        with mock.patch.dict(os.environ, {"WB_OPS_GENERATION_TIMEOUT": "300"}):
            stream = Stream(turn({"content": "answer"}))
            guarded = wb._GuardedStream(stream, {})
            self.assertEqual(len(list(guarded)), 3)
            guarded.close()
            self.assertFalse(guarded.timer.is_alive())
            self.assertFalse(guarded.expired.is_set())

    def test_empty_retry_shares_remaining_generation_time(self):
        class SlowStream(Stream):
            def __iter__(self):
                yield self.frames[0]
                time.sleep(.08)
                yield from self.frames[1:]
        for method in (self.response, self.chat):
            first = SlowStream(turn({"reasoning_content": "Checking the evidence."}))
            second = SlowStream(turn({"content": "partial answer"}))
            with mock.patch.dict(os.environ, {"WB_OPS_GENERATION_TIMEOUT": ".13"}), mock.patch.object(
                    wb, "open_upstream", return_value=(second, Account("a2"))) as opened:
                handler = method(first, BODY)
            self.assertEqual(opened.call_count, 1)
            self.assertIn(b"generation_timeout", b"".join(handler.written))
            self.assertTrue(first.closed and second.closed)

    def test_loop_opt_out_does_not_disable_deadline(self):
        class SlowStream(Stream):
            def __iter__(self):
                yield chunk({"reasoning_content": "Checking."})
                time.sleep(.06)
                yield chunk({"content": "late"}, "stop")
        with mock.patch.dict(os.environ, {"WB_OPS_LOOP_GUARD": "0", "WB_OPS_GENERATION_TIMEOUT": ".03"}):
            handler = self.chat(SlowStream([]), BODY)
        self.assertIn(b"generation_timeout", b"".join(handler.written))

    def test_nonstream_response_recovers_and_accounts(self):
        handler = Handler()
        with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn({"content": "answer"}, tokens=26)), Account("a2"))):
            handler._responses_nonstream_response(Stream(turn({"reasoning_content": "thinking"})), MODEL,
                                                   set(), {}, {}, Account(), time.time(), base_body=BODY)
        self.assertEqual(handler.json_body["status"], "completed")
        self.assertEqual(handler.json_body["usage"]["total_tokens"], 40)
        self.assertEqual({r[2].get("account") for r in self.records}, {"a1", "a2"})

    def test_nonstream_chat_recovers(self):
        handler = Handler()
        with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn({"content": "answer"})), Account("a2"))):
            handler._chat_nonstream_response(Stream(turn()), MODEL, {}, Account(), time.time(), base_body=BODY)
        self.assertEqual(handler.json_body["choices"][0]["message"]["content"], "answer")

    def test_daily_limit_is_flushed_before_retry_selection(self):
        pool = wb.wb_accounts.AccountPool(wb.ACCOUNTS_DIR)
        first = wb.wb_accounts.Account({"uid": "a1", "realm": "intl", "accessToken": "fake", "expiresAt": time.time() + 3600})
        second = wb.wb_accounts.Account({"uid": "a2", "realm": "intl", "accessToken": "fake", "expiresAt": time.time() + 3600})
        pool.accounts = [first, second]
        wb.wb_settings.set_daily_token_limit(wb.ACCOUNTS_DIR, 14)

        def open_next(body, **kwargs):
            self.assertTrue(first.daily_limit_blocked())
            picked = pool.pick(realm=kwargs.get("target_realm"), exclude=set(kwargs.get("exclude_uids") or []), model=MODEL)
            self.assertIs(picked, second)
            return Stream(turn({"content": "answer"}, tokens=26)), picked

        with mock.patch.object(wb, "POOL", pool), mock.patch.object(wb, "record_usage", REAL_RECORD), mock.patch.object(wb, "open_upstream", side_effect=open_next):
            handler = self.response(Stream(turn({"reasoning_content": "thinking"})), BODY, first)
        self.assert_lifecycle(handler)
        rows = [json.loads(line) for line in Path(wb.USAGE_LOG).read_text().splitlines()]
        self.assertEqual([(r["account"], r["total_tokens"]) for r in rows], [("a1", 14), ("a2", 26)])

    def test_retry_callback_preserves_realm_and_exclusions(self):
        callback = wb._make_retry_call(BODY, "session", "cn")
        with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn({"content": "answer"})), Account())) as opened:
            callback(["a1"], deadline_at=time.monotonic() + 1)
        kwargs = opened.call_args.kwargs
        self.assertEqual(kwargs["target_realm"], "cn")
        self.assertEqual(kwargs["exclude_uids"], ["a1"])
        self.assertEqual(kwargs["session_key"], "session")
        self.assertIn("deadline_at", kwargs)

    def test_probe_swaps_before_headers_and_preserves_first_chunk(self):
        first, second = Stream([b"data: [DONE]\n\n"]), Stream(turn({"content": "FIRST"}))
        with mock.patch.object(wb, "open_upstream", return_value=(second, Account("a2"))) as opened:
            final = self.assert_lifecycle(self.response(first, BODY))
        self.assertTrue(first.closed and second.closed)
        self.assertEqual(final["output_text"], "FIRST")
        self.assertIn("a1", opened.call_args.kwargs["exclude_uids"])

    def test_probe_preamble_is_bounded(self):
        stream = Stream([b": heartbeat\n"] * 600)
        with self.assertRaises(ValueError):
            wb.probe_first_frame(stream)
        self.assertTrue(stream.closed)

    def test_probe_content_rejection_does_not_swap_accounts(self):
        stream = Stream([b'data: {"error":{"code":11140,"message":"content rejected"}}\n\n'])
        callback = mock.Mock()
        with self.assertRaises(wb.ContentRejected):
            wb.probe_first_frame(stream, retry_call=callback, tried=["a1"])
        callback.assert_not_called()
        self.assertTrue(stream.closed)

    def test_continuation_rejection_is_reported_without_another_retry(self):
        with mock.patch.object(wb, "open_upstream", side_effect=wb.ContentRejected(detail="content rejected")) as opened:
            final = self.assert_lifecycle(self.response(Stream(turn({"reasoning_content": "thinking"})), BODY), "response.failed")
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(final["error"]["code"], "upstream_content_rejected")

    def test_retry_health_requires_key_and_does_not_return_key(self):
        handler = object.__new__(wb.Handler)
        handler.path, handler.headers = "/health/retry?limit=99999", {}
        result = []
        handler._json = lambda status, payload: result.append((status, payload))
        handler._error = lambda status, message, *a: result.append((status, {"error": message}))
        with mock.patch.multiple(wb, API_KEY="test-only-key", API_KEY_FILE_SET=False):
            handler.do_GET()
            self.assertEqual(result[-1][0], 401)
            handler.headers = {"Authorization": "Bearer test-only-key"}
            handler.do_GET()
            self.assertEqual(result[-1][0], 200)
        self.assertNotIn("test-only-key", json.dumps(result[-1][1]))
        self.assertTrue(wb.cors_origin_allowed("/health/retry"))
        self.assertFalse(wb.cors_origin_allowed("/v1/usage"))

    def test_real_http_headers_wait_for_first_frame(self):
        self.real_http(False)

    def test_real_http_bad_first_frames_return_error_not_200(self):
        self.real_http(True)

    def real_http(self, bad_first):
        class Origin(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(0.05)
                frames = [b"data: [DONE]\n\n"] if bad_first else turn({"content": "answer"})
                for frame in frames:
                    self.wfile.write(frame)
                    self.wfile.flush()
                self.close_connection = True

            def log_message(self, *args):
                pass

        class Gateway(wb.Handler):
            def _request_realm(self):
                return "intl"

            def _cross_realm_error(self, *args):
                return None

            _banned_model_error = _cross_realm_error
            _key_model_error = _cross_realm_error

            def log_message(self, *args):
                pass

        origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        workers = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
                   for server in (origin, gateway)]
        for worker in workers:
            worker.start()

        def open_local(body, **kwargs):
            request = urllib.request.Request("http://127.0.0.1:%d/" % origin.server_port,
                                             data=json.dumps(body).encode(), method="POST")
            return urllib.request.urlopen(request, timeout=2), Account(), "high"

        connection = http.client.HTTPConnection("127.0.0.1", gateway.server_port, timeout=3)
        try:
            with mock.patch.object(wb, "open_upstream", side_effect=open_local) as opened:
                started = time.monotonic()
                connection.request("POST", "/v1/chat/completions", json.dumps(BODY), {"Content-Type": "application/json"})
                response = connection.getresponse()
                elapsed = time.monotonic() - started
                payload = response.read()
            self.assertGreaterEqual(elapsed, 0.04)
            self.assertEqual(response.status, 502 if bad_first else 200)
            self.assertEqual(opened.call_count, wb.FIRST_FRAME_MAX_ATTEMPTS if bad_first else 1)
            if not bad_first:
                self.assertIn(b"answer", payload)
                self.assertEqual(payload.count(b"data: [DONE]"), 1)
        finally:
            connection.close()
            for server in (origin, gateway):
                server.shutdown()
                server.server_close()
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())

    def test_probe_blocked_socket_obeys_deadline(self):
        left, right = socket.socketpair()

        class SocketStream:
            fp = left.makefile("rb")

            def __iter__(self):
                return iter(self.fp)

            def close(self):
                self.fp.close()
                left.close()

        started = time.monotonic()
        try:
            with self.assertRaises((OSError, ValueError)):
                wb.probe_first_frame(SocketStream(), deadline_at=time.monotonic() + 0.1)
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            right.close()
            left.close()

    def test_web_rounds_have_one_lifecycle_and_no_index_reuse(self):
        body = dict(BODY, _web_tools=True, tools=[web.web_search_tool_def()])
        calls = {"tool_calls": [{"index": 0, "id": "w1", "function": {"name": "web_search", "arguments": '{"query":"cats"}'}}]}
        with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn({"content": "answer"})), Account("a2"), "high")), mock.patch.object(web, "execute", return_value="Search results for: cats\n1. Cats\n   https://example.com") as executed:
            final = self.assert_lifecycle(self.response(Stream(turn(calls, "tool_calls")), body))
        self.assertEqual(executed.call_count, 1)
        self.assertIn("web_search_call", [item["type"] for item in final["output"]])
        self.assertIn("message", [item["type"] for item in final["output"]])

    def test_web_misbehaving_after_withdrawal_fails_and_stops(self):
        body = dict(BODY, _web_tools=True, tools=[web.web_search_tool_def()])
        calls = {"tool_calls": [{"index": 0, "id": "w1", "function": {"name": "web_search", "arguments": '{"query":"cats"}'}}]}
        with mock.patch.object(wb, "open_upstream", side_effect=lambda *a, **k: (Stream(turn(calls, "tool_calls")), Account(), "high")), mock.patch.object(web, "execute", return_value="Search results for: cats") as executed:
            final = self.assert_lifecycle(self.response(Stream(turn(calls, "tool_calls")), body), "response.failed")
        self.assertEqual(final["error"]["code"], "web_tool_budget_exhausted")
        self.assertLessEqual(executed.call_count, web.MAX_WEB_ROUNDS + 1)

    def test_codec_and_bing_fallback(self):
        payload = b"<html>Python 3.12</html>"
        self.assertEqual(web._decompress(gzip.compress(payload), "gzip"), payload)
        self.assertEqual(web._decompress(zlib.compress(payload), "deflate"), payload)
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw = compressor.compress(payload) + compressor.flush()
        self.assertEqual(web._decompress(raw, "deflate"), payload)
        bing = '<li class="b_algo"><h2><a href="https://python.org">Python</a></h2><p>Release</p></li>'
        pages = [(b'<div class="anomaly-modal">challenge</div>', 200, 'text/html', 'utf-8'), (bing.encode(), 200, 'text/html', 'utf-8')]
        with mock.patch.object(web, "_open_url", side_effect=pages), mock.patch.object(web, "_chain_for", return_value=['duckduckgo','bing']), mock.patch.object(web, "_cooling", return_value=False), mock.patch.dict(os.environ, {"WB_OPS_BING_FALLBACK": "1"}):
            result = web.search("python release")
        self.assertIn("https://python.org", result)
        self.assertNotIn("No results", result)

    def test_codec_expansion_is_bounded(self):
        for encoded, codec in ((gzip.compress(b"x" * 1000), "gzip"), (zlib.compress(b"x" * 1000), "deflate")):
            with self.assertRaises(ValueError):
                web._decompress(encoded, codec, limit=64)

    def test_http_get_requests_only_supported_encodings(self):
        headers = email.message.Message()
        headers["Content-Encoding"] = "gzip"
        headers["Content-Type"] = "text/html; charset=utf-8"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = headers
        response.read.return_value = gzip.compress(b"hello")
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(web.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(web._open_url("https://example.com")[0], b"hello")
        self.assertNotIn("br", opener.open.call_args.args[0].get_header("Accept-encoding"))

    def test_custom_search_backend_does_not_leak_to_bing(self):
        with mock.patch.dict(os.environ, {"WB_SEARCH_ENDPOINT": "https://custom.example/search", "WB_OPS_BING_FALLBACK":"0"}), mock.patch.object(web, "_chain_for", return_value=['duckduckgo','bing']), mock.patch.object(web, "_cooling", return_value=False), mock.patch.object(web, "_run_engine", return_value={'state':'blocked','detail':'challenge'}) as run:
            web.search("private query")
        self.assertEqual([c.args[0] for c in run.call_args_list], ['duckduckgo'])


class LoopDetectorTests(unittest.TestCase):
    def test_fixture_chunk_sizes(self):
        for conservative in (False, True):
            for size in (1, 7, 64, 1024, len(ROTATION)):
                guard = wb.ReasoningLoopGuard(conservative=conservative)
                for offset in range(0, len(ROTATION), size):
                    if guard.feed(ROTATION[offset:offset + size]):
                        break
                self.assertTrue(guard.hit, (conservative, size))
                if size < 1024:
                    self.assertLess(offset + size, len(ROTATION) * .8)

    def test_inline_case_and_punctuation_variants(self):
        text = ("LET ME WRITE IT! Go. writing the tool call? OKAY. Let me output it. Now. " * 20)
        for conservative in (False, True):
            self.assertTrue(wb.ReasoningLoopGuard(conservative).feed(text))

    def test_intervening_long_sentence_does_not_mask_churn(self):
        text = ROTATION.replace("Final.", "I need to stop deliberating and emit the call because this line is only another statement of intent.")
        self.assertTrue(wb.ReasoningLoopGuard().feed(text))

    def test_fenced_code_tables_lists_and_progress_are_not_loops(self):
        bodies = ["```text\n" + ROTATION * 4 + "\n```\nDone.",
                  "~~~text\n" + ROTATION * 4 + "\n~~~\nDone.",
                  "````markdown\n```text\n" + ROTATION * 4 + "\n```\n````\nDone.",
                  ("| go | write | now |\n" * 100),
                  ("- Go.\n- Let me write.\n" * 100),
                  ("print('Go. Let me write.')\n" * 100),
                  "\n".join("Let me inspect component %d for a different failure." % i for i in range(200)),
                  "\n".join("Let me inspect the %s." % name for name in (
                      "parser", "router", "server", "handler", "cache", "database", "queue", "client", "view", "model") )]
        for conservative in (False, True):
            for body in bodies:
                for size in (1, 128, len(body)):
                    guard = wb.ReasoningLoopGuard(conservative)
                    for i in range(0, len(body), size):
                        guard.feed(body[i:i + size])
                    self.assertFalse(guard.hit, (conservative, size, body[:30], guard.describe()))

    def test_plain_content_requires_stalling_language(self):
        self.assertFalse(wb.ReasoningLoopGuard(conservative=True).feed("Status is healthy.\n" * 100))

    def test_long_code_line_keeps_protected_state_after_tail_rollover(self):
        body = 'print("' + "Go. Let me write. Now. " * 500 + '")\n'
        for size in (1, 128, len(body)):
            guard = wb.ReasoningLoopGuard()
            for i in range(0, len(body), size):
                guard.feed(body[i:i + size])
            self.assertFalse(guard.hit)

    def test_code_then_actual_churn_is_detected(self):
        guard = wb.ReasoningLoopGuard(conservative=True)
        self.assertFalse(guard.feed("```text\n" + ROTATION + "\n```\n"))
        self.assertTrue(guard.feed(ROTATION))

    def test_opt_out_and_bounded_buffers(self):
        with mock.patch.dict(os.environ, {"WB_OPS_LOOP_GUARD": "0"}):
            self.assertFalse(wb.ReasoningLoopGuard().feed(ROTATION))
        guard = wb.ReasoningLoopGuard()
        guard.feed("x" * 200000)
        self.assertLessEqual(len(guard.line), wb.REASON_LOOP_TAIL)
        self.assertLessEqual(len(guard.tail), wb.REASON_LOOP_TAIL)

    def test_deadline_configuration(self):
        for value in ("nan", "inf", "-1", "invalid"):
            with mock.patch.dict(os.environ, {"WB_OPS_GENERATION_TIMEOUT": value}):
                now = time.monotonic()
                deadline = wb._generation_deadline({})
                self.assertGreaterEqual(deadline - now, 299)
                self.assertLess(deadline - now, 301)
        with mock.patch.dict(os.environ, {"WB_OPS_GENERATION_TIMEOUT": "0"}):
            self.assertIsNone(wb._generation_deadline({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)

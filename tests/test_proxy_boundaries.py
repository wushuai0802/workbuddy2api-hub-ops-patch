"""Synthetic refusal, history and single-candidate lifecycle boundaries."""
import http.client
from http.server import ThreadingHTTPServer
import json
import os
import time
import threading
import unittest
from unittest import mock

import test_integration as integration
from test_integration import Account, BODY, Handler, MODEL, Stream, chunk, responses, turn, wb


REFUSAL = "I cannot complete this request."
MIXED = ("Hmm.\nHmm.\nLet me go.\n"
         "The candidate needs another comparison before a concrete result.\n") * 80
PATHS = [(protocol, stream) for protocol in ("chat", "responses", "messages") for stream in (True, False)]


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        integration.IntegrationTests.setUp(self)
        self.enterContext(mock.patch.dict(os.environ, {
            "WB_OPS_LOOP_RETRY": "1", "WB_OPS_LOOP_GUARD": "1",
            "WB_OPS_MIXED_STALL_GUARD": "1", "WB_OPS_LEAKED_THINKING_GUARD": "1",
            "WB_OPS_GENERATION_TIMEOUT": "30",
        }))

    def invoke(self, protocol, stream, frames):
        handler = Handler("/v1/" + {"chat": "chat/completions", "responses": "responses", "messages": "messages"}[protocol])
        upstream = Stream(frames)
        method = getattr(handler, "_" + protocol + ("_stream_response" if stream else "_nonstream_response"))
        args = (upstream, MODEL)
        if protocol == "responses":
            args += (set(), {})
        method(*args, {"hash": "test"}, Account(), time.time(), base_body=BODY)
        self.assertTrue(upstream.closed)
        return handler

    def assert_refusal(self, handler, protocol, stream, finish):
        if protocol == "chat":
            if stream:
                frames = responses(handler)
                choices = [c for f in frames for c in f.get("choices", [])]
                self.assertEqual("".join(c["delta"].get("refusal", "") for c in choices), REFUSAL)
                terminals = [c for c in choices if c.get("finish_reason")]
                self.assertEqual([c["finish_reason"] for c in terminals], [finish])
                self.assertEqual(b"".join(handler.written).count(b"[DONE]"), 1)
                self.assertFalse(any(f.get("error") for f in frames))
            else:
                result = handler.json_body
                self.assertEqual(result["choices"][0]["message"]["refusal"], REFUSAL)
                self.assertEqual(result["choices"][0]["finish_reason"], finish)
        elif protocol == "responses":
            if stream:
                events = responses(handler)
                terminals = [e for e in events if e["type"] in (
                    "response.completed", "response.failed", "response.incomplete")]
                self.assertEqual(len(terminals), 1)
                self.assertEqual(terminals[0]["type"], "response.incomplete" if finish == "content_filter" else "response.completed")
                result = terminals[0]["response"]
                self.assertEqual("".join(e["delta"] for e in events if e["type"] == "response.refusal.delta"), REFUSAL)
                self.assertEqual(sum(e["type"] == "response.refusal.done" for e in events), 1)
                self.assertEqual(sum(e["type"] == "response.created" for e in events), 1)
                self.assertEqual([e["sequence_number"] for e in events], list(range(1, len(events) + 1)))
            else:
                result = handler.json_body
            self.assertEqual(result["status"], "incomplete" if finish == "content_filter" else "completed")
            if finish == "content_filter":
                self.assertEqual(result["incomplete_details"], {"reason": "content_filter"})
            parts = [p for item in result["output"] if item["type"] == "message" for p in item["content"]]
            self.assertEqual([p["refusal"] for p in parts if p["type"] == "refusal"], [REFUSAL])
            self.assertEqual(result["output_text"], "")
        else:
            if stream:
                events = responses(handler)
                self.assertEqual("".join(e["delta"].get("text", "") for e in events if e["type"] == "content_block_delta"), REFUSAL)
                self.assertEqual(sum(e["type"] == "message_start" for e in events), 1)
                self.assertEqual(sum(e["type"] == "message_stop" for e in events), 1)
                self.assertEqual([e["delta"]["stop_reason"] for e in events if e["type"] == "message_delta"], ["refusal"])
                self.assertFalse(any(e["type"] == "error" for e in events))
            else:
                self.assertEqual(handler.json_body["content"], [{"type": "text", "text": REFUSAL}])
                self.assertEqual(handler.json_body["stop_reason"], "refusal")

    def test_refusal_stop_and_content_filter_all_six_paths(self):
        for protocol, stream in PATHS:
            for finish in ("stop", "content_filter"):
                for reasoning in (False, True):
                    with self.subTest(protocol=protocol, stream=stream, finish=finish, reasoning=reasoning):
                        delta = {"refusal": REFUSAL}
                        if reasoning:
                            delta["reasoning_content"] = "The requested action cannot be performed."
                        with mock.patch.object(wb, "open_upstream") as opened:
                            handler = self.invoke(protocol, stream, turn(delta, finish=finish))
                        opened.assert_not_called()
                        self.assert_refusal(handler, protocol, stream, finish)

    def test_refusal_blocks_reasoning_answer_recovery_even_on_terminal_frame(self):
        trace = "Analysis complete.\n</thinking>\nA fabricated recovery answer must never replace refusal."
        frames = [chunk({"reasoning_content": trace, "refusal": REFUSAL}, finish="stop"), b"data: [DONE]\n\n"]
        for protocol, stream in PATHS:
            with self.subTest(protocol=protocol, stream=stream), mock.patch.object(wb, "open_upstream") as opened:
                handler = self.invoke(protocol, stream, frames)
                self.assert_refusal(handler, protocol, stream, "stop")
                if protocol == "chat" and not stream:
                    self.assertEqual(handler.json_body["choices"][0]["message"]["content"], "")
            opened.assert_not_called()

    def test_refusal_blocks_later_loop_replay(self):
        for protocol, stream in PATHS:
            with self.subTest(protocol=protocol, stream=stream), mock.patch.object(wb, "open_upstream") as opened:
                handler = self.invoke(protocol, stream, [chunk({"refusal": REFUSAL})] + turn({"reasoning_content": MIXED}))
                raw = json.dumps(handler.json_body) if not stream else b"".join(handler.written).decode()
                self.assertIn("reasoning_loop_detected", raw)
            opened.assert_not_called()

    def test_refusal_and_normal_text_keep_distinct_response_parts(self):
        frames = [chunk({"refusal": REFUSAL[:12]}), chunk({"content": "Visible detail."}),
                  chunk({"refusal": REFUSAL[12:]}), chunk(finish="stop"), b"data: [DONE]\n\n"]
        for stream in (True, False):
            with self.subTest(stream=stream), mock.patch.object(wb, "open_upstream") as opened:
                handler = self.invoke("responses", stream, frames)
            opened.assert_not_called()
            result = next(e["response"] for e in responses(handler) if e["type"] == "response.completed") if stream else handler.json_body
            parts = [p for item in result["output"] if item["type"] == "message" for p in item["content"]]
            self.assertEqual(next(p["refusal"] for p in parts if p["type"] == "refusal"), REFUSAL)
            self.assertEqual(next(p["text"] for p in parts if p["type"] == "output_text"), "Visible detail.")
            if stream:
                events = responses(handler)
                starts = [e for e in events if e["type"] == "response.content_part.added"]
                self.assertEqual([e["part"]["type"] for e in starts], ["refusal", "output_text"])
                self.assertEqual([e["content_index"] for e in starts], [0, 1])
                for event in events:
                    if event["type"] == "response.output_text.delta":
                        self.assertEqual(event["content_index"], 1)
                    if event["type"] == "response.refusal.delta":
                        self.assertEqual(event["content_index"], 0)

    def test_invalid_n_requests_are_rejected_before_any_upstream(self):
        for value in (0, 2, -1, True, False, None, "1", 1.0):
            for route in ("chat", "responses", "messages"):
                with self.subTest(value=value, route=route), mock.patch.object(wb, "open_upstream") as opened:
                    handler = Handler()
                    payload = dict(BODY, n=value)
                    if route == "messages":
                        result = wb.Handler._handle_messages(handler, payload)
                    elif route == "responses":
                        result = wb.Handler._handle_responses(handler, payload)
                    else:
                        result = wb.Handler._dispatch_chat_post(handler, "/v1/chat/completions", payload)
                    self.assertEqual(result[0], 400)
                    self.assertIn("n=1", result[1]["error"]["message"])
                opened.assert_not_called()
        self.assertIsNone(wb._single_candidate_error(BODY))
        self.assertIsNone(wb._single_candidate_error(dict(BODY, n=1)))
        with mock.patch.object(wb.wb_accounts, "urlopen") as opened:
            with self.assertRaisesRegex(ValueError, "n=1"):
                wb.open_upstream(dict(BODY, n=2))
        opened.assert_not_called()

    def test_real_http_n_rejection_all_generation_routes(self):
        class Gateway(wb.Handler):
            def _authorized(self):
                return True

            def _key_ok(self):
                return True

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with mock.patch.object(wb, "open_upstream") as opened:
                for path in ("/v1/chat/completions", "/v1/responses", "/v1/messages"):
                    with self.subTest(path=path):
                        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                        try:
                            body = json.dumps(dict(BODY, n=2)).encode()
                            client.request("POST", path, body=body, headers={"Content-Type": "application/json"})
                            response = client.getresponse()
                            self.assertEqual(response.status, 400)
                            self.assertIn("n=1", json.loads(response.read())["error"]["message"])
                        finally:
                            client.close()
            opened.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)
            self.assertFalse(worker.is_alive())

    def test_independent_choices_fail_explicitly_without_detector_or_replay(self):
        short = "Let me write the actual tool call.\n" * 6
        guard = wb.ReasoningLoopGuard()
        self.assertFalse(guard.feed(short))
        choice0 = {"index": 0, "delta": {"reasoning_content": short}, "finish_reason": None}
        choice1 = dict(choice0, index=1)
        same = [("data: " + json.dumps({"choices": [choice0, choice1]}) + "\n\n").encode()]
        interleaved = [chunk({"reasoning_content": short}),
                       ("data: " + json.dumps({"choices": [choice1]}) + "\n\n").encode()]
        for frames in (same, interleaved):
            for protocol, stream in PATHS:
                with self.subTest(frames=len(frames), protocol=protocol, stream=stream), mock.patch.object(wb, "open_upstream") as opened:
                    handler = self.invoke(protocol, stream, frames)
                    raw = json.dumps(handler.json_body) if not stream else b"".join(handler.written).decode()
                    self.assertIn("upstream_choices_unsupported", raw)
                    self.assertNotIn("reasoning_loop_detected", raw)
                opened.assert_not_called()

    def test_tagged_mixed_stall_fails_all_six_paths_without_replay(self):
        tagged = "<thinking>\n" + MIXED + "</thinking>\nA final answer."
        frames = [chunk({"content": tagged[i:i + 47]}) for i in range(0, len(tagged), 47)] + turn()
        for protocol, stream in PATHS:
            with self.subTest(protocol=protocol, stream=stream), mock.patch.object(wb, "open_upstream") as opened:
                handler = self.invoke(protocol, stream, frames)
                raw = json.dumps(handler.json_body) if not stream else b"".join(handler.written).decode()
                self.assertIn("content_loop_detected", raw)
            opened.assert_not_called()

    def test_tagged_final_sentence_flushes_on_finish_and_eof_without_retry(self):
        body = "".join("Hmm. Hmm.\nEvidence at position %d.\n" % i for i in range(32)) + "Let me go.\nI will run"
        for ending in ([], [chunk(finish="stop"), b"data: [DONE]\n\n"]):
            frames = [chunk({"content": "<thinking>" + body})] + ending
            for protocol, stream in PATHS:
                with self.subTest(ending=len(ending), protocol=protocol, stream=stream), mock.patch.object(wb, "open_upstream") as opened:
                    handler = self.invoke(protocol, stream, frames)
                    raw = json.dumps(handler.json_body) if not stream else b"".join(handler.written).decode()
                    self.assertIn("content_loop_detected", raw)
                opened.assert_not_called()

    def test_reasoning_and_leaked_content_do_not_join_mixed_evidence(self):
        body = "Let me go. Hmm. Hmm. Hmm. A brief fact.\n" * 4
        self.assertIsNotNone(wb.MixedStallGuard().feed(body * 2, final=True))
        original = [chunk({"reasoning_content": body, "content": "<thinking>" + body + "</thinking>"}),
                    chunk(finish="stop"), b"data: [DONE]\n\n"]
        holder = {"model": MODEL, "base_body": BODY}
        guarded = wb._GuardedStream(Stream(original), holder)
        try:
            self.assertEqual(list(guarded), original)
            self.assertNotIn("loop_evidence", holder)
        finally:
            guarded.close()

    def test_normal_quoted_and_fenced_thinking_text_pass_unchanged(self):
        for text in ("A normal answer. " + MIXED,
                     '"<thinking>"\n' + MIXED,
                     "```xml\n<thinking>\n" + MIXED + "</thinking>\n```",
                     "<thinking>Useful bounded analysis.</thinking>\n" + MIXED):
            with self.subTest(prefix=text[:50]):
                original = [chunk({"content": text}), chunk(finish="stop")]
                guarded = wb._GuardedStream(Stream(original), {"model": MODEL, "base_body": BODY})
                try:
                    self.assertEqual(list(guarded), original)
                finally:
                    guarded.close()


if __name__ == "__main__":
    unittest.main()

"""R3 early detection, one safe retry, exact history quarantine and telemetry."""
import copy
import json
import os
from pathlib import Path
import time
import unittest
from unittest import mock

import test_integration as integration
from test_integration import Account, BODY, Handler, MODEL, Stream, chunk, responses, turn, wb
from wb_ops import FailedReasoningCache, FastStallGuard, ReasoningLoopGuard

LOOP = ("Let me run.\nLet me write it.\nLet me go.\nOK.\nLet me do it.\nLet me emit.\n" * 50)
GOOD_CALL = {"tool_calls": [{"index": 0, "id": "call_good", "function": {"name": "lookup", "arguments": "{}"}}]}


def loop_frames(size=17, channel="reasoning_content"):
    return [chunk({channel: LOOP[i:i+size]}) for i in range(0, len(LOOP), size)]


class DetectionTests(unittest.TestCase):
    def test_fast_exact_and_normalized_periods(self):
        for width in (1, 2, 3, 6, 8):
            phrases = ["Let me run.", "Let me write it.", "Let me go.", "OK.", "Let me do it.", "Let me emit.", "Let me read.", "Let me output."][:width]
            text = "\n".join(phrases * 40) + "\n"
            fast = FastStallGuard()
            hits = [fast.feed_line(line) for line in text.splitlines()]
            self.assertTrue(any(hits), width)

    def test_stream_chunk_invariance(self):
        offsets = []
        for size in (1, 2, 17, 128, 1024):
            guard = ReasoningLoopGuard()
            for i in range(0, len(LOOP), size):
                if guard.feed(LOOP[i:i+size]):
                    offsets.append(i+size)
                    break
            self.assertTrue(guard.hit)
            self.assertLess(offsets[-1], 1200)
        self.assertLessEqual(max(offsets) - min(offsets), 1024)

    def test_fast_negative_examples(self):
        examples = [
            "Let me run a different regression for each case.\n" * 10,
            ("Let me run.\nThe query returned new information about the dependency.\n" * 30),
            "Let me run.\n" * 5,
        ]
        for text in examples:
            detector = FastStallGuard()
            self.assertFalse(any(detector.feed_line(line) for line in text.splitlines()))

    def test_no_newline_short_intents_and_go_only(self):
        for text in (LOOP.replace("\n", " "), "Go.\n" * 60, "OK. " * 60):
            for size in (1, 17, 128, 1024):
                guard = ReasoningLoopGuard()
                for i in range(0, len(text), size):
                    if guard.feed(text[i:i+size]):
                        break
                self.assertTrue(guard.hit, (text[:50], size))
                self.assertTrue(guard.rule.startswith("fast"))

    def test_code_math_table_and_block_quotes_are_excluded(self):
        negatives = ["```\n" + LOOP + "\n```", "~~~\n" + LOOP + "\n~~~",
                     "\n".join("> " + line for line in LOOP.splitlines()),
                     ("print('Let me run.')\n" * 50), ("a(n) = n**2 + 1\n" * 50),
                     ("| Let me run | OK |\n" * 50)]
        for text in negatives:
            for size in (1, 17, 1024):
                guard = ReasoningLoopGuard()
                for i in range(0, len(text), size):
                    self.assertFalse(guard.feed(text[i:i+size]), text[:50])

    def test_progress_and_code_reset_fast_window(self):
        for separator in ("New independent evidence was found.\n", "x = 12\n", "```\ncode\n```\n"):
            text = "".join(("Let me run.\n" * 6) + separator.replace("found.", "found in case %d." % n) for n in range(8))
            guard = ReasoningLoopGuard()
            for line in text.splitlines(keepends=True):
                self.assertFalse(guard.feed(line))


class RecoveryTests(unittest.TestCase):
    response = integration.IntegrationTests.response
    chat = integration.IntegrationTests.chat
    assert_lifecycle = integration.IntegrationTests.assert_lifecycle

    def setUp(self):
        integration.IntegrationTests.setUp(self)
        self.enterContext(mock.patch.dict(os.environ, {"WB_OPS_LOOP_RETRY": "1", "WB_OPS_FAILED_REASONING_CLEANUP": "1"}))
        self.enterContext(mock.patch.object(wb, "_FAILED_REASONING", FailedReasoningCache()))
        with wb._LOOP_STATS_LOCK:
            wb._LOOP_STATS.clear()
            wb._LOOP_EVENTS.clear()

    def invoke(self, api, stream, body=BODY, model=MODEL):
        handler = Handler()
        args = (stream, model)
        if api == "chat":
            handler._chat_stream_response(*args, {}, Account(), time.time(), base_body=body)
        elif api == "responses":
            handler._responses_stream_response(*args, set(), {}, {}, Account(), time.time(), base_body=body)
        elif api == "chat_json":
            handler._chat_nonstream_response(*args, {}, Account(), time.time(), base_body=body)
        else:
            handler._responses_nonstream_response(*args, set(), {}, {}, Account(), time.time(), base_body=body)
        return handler

    def assert_success(self, api, h, tools=False):
        if api == "responses":
            final = self.assert_lifecycle(h)
            if tools:
                self.assertEqual(sum(x["type"] == "function_call" for x in final["output"]), 1)
            else:
                self.assertEqual(final["output_text"], "answer")
        elif api == "chat":
            frames = responses(h)
            self.assertFalse(any("error" in f for f in frames))
            self.assertEqual(len({f["id"] for f in frames}), 1)
            self.assertEqual(sum(bool(c.get("finish_reason")) for f in frames for c in f.get("choices", [])), 1)
            self.assertEqual(b"".join(h.written).count(b"[DONE]"), 1)
        else:
            self.assertNotIn("error", h.json_body)
            if api == "responses_json":
                self.assertEqual(h.json_body["status"], "completed")
            else:
                msg = h.json_body["choices"][0]["message"]
                self.assertEqual(msg.get("content"), "answer" if not tools else "")

    def test_one_loop_then_answer_all_four_apis(self):
        for api in ("chat", "responses", "chat_json", "responses_json"):
            with self.subTest(api=api):
                first, second = Stream(loop_frames()), Stream(turn({"content": "answer"}, tokens=26))
                with mock.patch.object(wb, "open_upstream", return_value=(second, Account("a2"))) as opened:
                    h = self.invoke(api, first)
                self.assert_success(api, h)
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(opened.call_args.kwargs["exclude_uids"], ["a1"])
                self.assertEqual(opened.call_args.args[0], BODY)  # failed text never appended
                self.assertTrue(first.closed and second.closed)

    def test_retry_may_return_valid_tools(self):
        for api in ("chat", "responses", "chat_json", "responses_json"):
            with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn(GOOD_CALL, "tool_calls")), Account("b"))):
                self.assert_success(api, self.invoke(api, Stream(loop_frames())), tools=True)

    def test_single_account_is_not_excluded_but_multiple_accounts_rotate(self):
        for count in (1, 2):
            pool = mock.Mock()
            pool.count_ready.return_value = count
            with mock.patch.object(wb, "POOL", pool), mock.patch.object(wb, "open_upstream", return_value=(Stream(turn({"content": "answer"})), Account("b"))) as opened:
                self.assert_success("chat", self.invoke("chat", Stream(loop_frames())))
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(opened.call_args.kwargs["exclude_uids"], [] if count == 1 else ["a1"])

    def test_retry_usage_includes_first_attempt_and_accounting_precedes_open(self):
        first = Stream([chunk(usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14})] + loop_frames())
        def opened(*args, **kwargs):
            self.assertTrue(any(r[2].get("account") == "a1" for r in self.records))
            self.assertTrue(first.closed)
            return Stream(turn({"content": "answer"}, tokens=26)), Account("a2")
        with mock.patch.object(wb, "open_upstream", side_effect=opened):
            final = self.assert_lifecycle(self.invoke("responses", first))
        self.assertEqual(final["usage"]["total_tokens"], 40)

    def test_loop_loop_is_one_retry_with_one_terminal_error(self):
        for api in ("chat", "responses", "chat_json", "responses_json"):
            with mock.patch.object(wb, "open_upstream", return_value=(Stream(loop_frames()), Account("b"))) as opened:
                h = self.invoke(api, Stream(loop_frames()))
            self.assertEqual(opened.call_count, 1)
            if api == "responses":
                self.assertEqual(self.assert_lifecycle(h, "response.failed")["error"]["code"], "reasoning_loop_detected")
            elif api == "chat":
                self.assertEqual(sum("error" in f for f in responses(h)), 1)
            else:
                self.assertIn("reasoning_loop_detected", json.dumps(h.json_body))

    def test_empty_and_loop_share_one_retry_budget(self):
        for first_frames, second_frames in ((turn({"reasoning_content": "ordinary thought"}), loop_frames()),
                                             (loop_frames(), turn({"reasoning_content": "ordinary thought"}))):
            for api in ("chat", "responses", "chat_json", "responses_json"):
                with mock.patch.object(wb, "open_upstream", return_value=(Stream(second_frames), Account("b"))) as opened:
                    self.invoke(api, Stream(first_frames))
                self.assertEqual(opened.call_count, 1)

    def test_content_and_any_tool_fragment_block_retry(self):
        prefixes = [{"content": "partial"}, GOOD_CALL,
                    {"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]},
                    {"function_call": {"arguments": "{"}},
                    {"reasoning_content": "<DSML calls><DSML invoke name='lookup'>x</DSML invoke></DSML calls>"}]
        for prefix in prefixes:
            for api in ("chat", "responses", "chat_json", "responses_json"):
                with self.subTest(api=api, prefix=prefix), mock.patch.object(wb, "open_upstream") as opened:
                    self.invoke(api, Stream([chunk(prefix)] + loop_frames()))
                opened.assert_not_called()

    def test_same_frame_tool_cannot_be_replayed(self):
        delta = dict(GOOD_CALL, reasoning_content=LOOP)
        for api in ("chat", "responses", "chat_json", "responses_json"):
            with mock.patch.object(wb, "open_upstream") as opened:
                self.invoke(api, Stream([chunk(delta)]))
            opened.assert_not_called()

    def test_dsml_word_is_not_a_call_but_split_envelope_is(self):
        for api in ("chat", "responses", "chat_json", "responses_json"):
            with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn({"content": "answer"})), Account("b"))) as opened:
                h = self.invoke(api, Stream([chunk({"reasoning_content": "The DSML safeguards need review.\n"})] + loop_frames()))
            self.assertEqual(opened.call_count, 1)
            self.assert_success(api, h)
            with mock.patch.object(wb, "open_upstream") as opened:
                self.invoke(api, Stream([chunk({"reasoning_content": x}) for x in ("<DS", "ML calls>", "</DSML calls>\n")] + loop_frames()))
            opened.assert_not_called()

    def test_no_retry_for_content_loop_timeout_truncation_length_or_other_model(self):
        samples = [(loop_frames(channel="content"), MODEL),
                   ([chunk({"reasoning_content": "short"})], MODEL),
                   (turn({"reasoning_content": "short"}, "length"), MODEL),
                   ([chunk({"reasoning_content": "starting"}), b'data: {"error":{"code":"generation_timeout"}}\n\n'], MODEL),
                   (loop_frames(), "another-model")]
        for frames, model in samples:
            for api in ("chat", "responses", "chat_json", "responses_json"):
                with mock.patch.object(wb, "open_upstream") as opened:
                    self.invoke(api, Stream(frames), model=model)
                opened.assert_not_called()

    def test_opt_out_and_multi_choice(self):
        for body, env in [(BODY, {"WB_OPS_LOOP_RETRY": "off"}), (dict(BODY, n=2), {})]:
            with mock.patch.dict(os.environ, env), mock.patch.object(wb, "open_upstream") as opened:
                self.invoke("chat", Stream(loop_frames()), body=body)
            opened.assert_not_called()

    def test_probe_failure_is_not_a_second_loop_retry(self):
        second = Stream([])
        with mock.patch.object(wb, "open_upstream", return_value=(second, Account("b"))) as opened:
            h = self.invoke("responses", Stream(loop_frames()))
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.assert_lifecycle(h, "response.failed")["error"]["code"], "continuation_probe_failed")
        self.assertTrue(second.closed)

    def test_retry_timeout_and_rejection_remain_errors(self):
        for exc, code in [(TimeoutError("private error"), "continuation_timeout"),
                          (RuntimeError("private error"), "continuation_request_failed")]:
            with mock.patch.object(wb, "open_upstream", side_effect=exc):
                h = self.invoke("responses", Stream(loop_frames()))
            self.assertEqual(self.assert_lifecycle(h, "response.failed")["error"]["code"], code)
            self.assertNotIn(b"private error", b"".join(h.written))

    def test_shared_generation_deadline_and_exhausted_budget(self):
        holder = {"generation_deadline": time.monotonic() - 1, "retry_call": mock.Mock(),
                  "loop_evidence": {"rule": "exact"}}
        self.assertFalse(wb._try_loop_continuation(holder, MODEL, "reasoning_loop_detected", LOOP))
        holder["retry_call"].assert_not_called()
        self.assertEqual(holder["incomplete_reason"], "generation_timeout")
        deadline = time.monotonic() + 30
        holder = {"generation_deadline": deadline, "retry_call": lambda *a: (Stream(turn({"content": "answer"})), Account("b")),
                  "loop_evidence": {"rule": "exact"}}
        self.assertTrue(wb._try_loop_continuation(holder, MODEL, "reasoning_loop_detected", LOOP))
        self.assertEqual(holder["generation_deadline"], deadline)
        holder["continue_upstream"].close()

    def test_client_disconnect_does_not_start_retry(self):
        h = Handler(writer_error=BrokenPipeError("gone"))
        first = Stream(loop_frames())
        with mock.patch.object(wb, "open_upstream") as opened:
            h._responses_stream_response(first, MODEL, set(), {}, {}, Account(), time.time(), base_body=BODY)
        opened.assert_not_called()
        self.assertTrue(first.closed)

    def test_hash_quarantine_matches_streamed_prefix_not_successful_reasoning(self):
        good_reason = "The actual successful lookup reasoning."
        with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn(dict(GOOD_CALL, reasoning_content=good_reason), "tool_calls")), Account("b"))):
            h = self.invoke("chat", Stream(loop_frames()))
        reasoning = "".join(c.get("delta", {}).get("reasoning_content", "") for f in responses(h) for c in f.get("choices", []))
        original = [{"role": "assistant", "content": "", "tool_calls": GOOD_CALL["tool_calls"], "reasoning_content": reasoning}]
        before = copy.deepcopy(original)
        cleaned = wb.backfill_reasoning_content(original, MODEL, True)
        self.assertEqual(original, before)
        self.assertEqual(cleaned[0]["reasoning_content"], good_reason)
        self.assertEqual(cleaned[0]["tool_calls"], original[0]["tool_calls"])
        self.assertEqual(cleaned[0]["reasoning"], good_reason)

    def test_unknown_loop_history_is_not_deleted(self):
        original = [{"role": "assistant", "reasoning_content": LOOP, "content": "answer"}]
        self.assertEqual(wb.backfill_reasoning_content(original, MODEL, True)[0]["reasoning_content"], LOOP)

    def test_telemetry_has_no_transcript_and_is_bounded(self):
        with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn({"content": "answer"})), Account("b"))):
            self.invoke("responses", Stream(loop_frames()))
        report = wb.retry_health_snapshot()
        manifest = json.loads((Path(__file__).resolve().parents[1] / "BASELINE.json").read_text())
        self.assertEqual(report["ops_patch_id"], manifest["patch_id"])
        self.assertEqual(report["loops"]["counts"]["retry_recovered"], 1)
        self.assertEqual(report["loops"]["counts"]["retry_started"], 1)
        self.assertNotIn("Let me", json.dumps(report["loops"]))
        for _ in range(80):
            wb._loop_event("retry_started")
        self.assertEqual(len(wb.loop_health_snapshot()["recent"]), 64)


class CacheTests(unittest.TestCase):
    def test_ttl_exact_prefix_bounds_and_no_raw_text(self):
        now = [0]
        cache = FailedReasoningCache(limit=2, ttl=10, clock=lambda: now[0])
        cache.remember(LOOP[:300])
        self.assertEqual(cache.clean(LOOP[:300] + "answer"), "answer")
        self.assertEqual(cache.clean("prefix" + LOOP[:300]), "prefix" + LOOP[:300])
        self.assertNotIn("Let me", repr(cache.entries))
        now[0] = 10
        self.assertEqual(cache.clean(LOOP[:300]), LOOP[:300])
        self.assertEqual(len(cache.entries), 0)
        for i in range(3):
            cache.remember(str(i) * 300)
        self.assertEqual(len(cache.entries), 2)
        cache.remember("x" * 65537)
        self.assertEqual(len(cache.entries), 2)


if __name__ == "__main__":
    unittest.main()

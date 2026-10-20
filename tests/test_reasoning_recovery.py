"""Bounded answer recovery, protocol lifecycles and failed-retry diagnostics."""
import json
import os
import time
import unittest
from unittest import mock

import test_integration as integration
from test_integration import Account, BODY, Handler, MODEL, Stream, chunk, responses, turn, wb


ANSWER = "## 检查结果\n\n已完成当前检查，结果正常。\n\n| 项目 | 状态 |\n|---|---|\n| 连接 | 正常 |"
# Sanitized structure from the observed Pi failure; no task data or credentials.
MIXED = "\x1b[38;2;194;198;216mThe check has finished.\nLet me give the final summary.\n\nGo.</thinking>\n" + ANSWER + "\x1b[39m"


class RecoveryTests(unittest.TestCase):
    setUp = integration.IntegrationTests.setUp
    response = integration.IntegrationTests.response
    chat = integration.IntegrationTests.chat
    assert_lifecycle = integration.IntegrationTests.assert_lifecycle

    def recover(self, text, size=17, model=MODEL):
        recovery = wb.ReasoningAnswerRecovery(model)
        for i in range(0, len(text), size):
            recovery.feed(text[i:i + size])
        return recovery.answer()

    def test_all_tag_chunk_boundaries(self):
        for size in (1, 2, 5, 17, 128, 4096):
            with self.subTest(size=size):
                self.assertEqual(self.recover(MIXED, size), ANSWER)

    def test_explicit_boundary_variants(self):
        for prefix, tag, sep in [("", "</think>", "\n"), ("Let me write.", "</thinking>", "\n"),
                                 ("Now write the answer.", "</thinking>", ""), ("", "</THINKING>", "\r\n")]:
            with self.subTest(prefix=prefix, tag=tag):
                self.assertEqual(self.recover("Analysis complete.\n" + prefix + tag + sep + ANSWER), ANSWER)

    def test_quoted_code_and_ambiguous_markers_are_not_recovered(self):
        negatives = [
            "Reasoning contains a useful draft but no boundary. " + ANSWER,
            "```text\nGo.</thinking>\n" + ANSWER + "\n```",
            "````text\n```\nGo.</thinking>\n" + ANSWER + "\n````",
            "~~~text\nGo.</thinking>\n" + ANSWER + "\n~~~",
            "> </thinking>\n" + ANSWER,
            "    </thinking>\n" + ANSWER,
            "\t</thinking>\n" + ANSWER,
            'The string "</thinking>" means a closing tag.\n' + ANSWER,
            "The literal tag follows:\n</thinking>\n" + ANSWER,
            "使用这个标签：\n</thinking>\n" + ANSWER,
            "`</thinking>`\n" + ANSWER,
            '\"\"\"\n</thinking>\n' + ANSWER + '\n\"\"\"',
            "\\</thinking>\n" + ANSWER,
            "</thinking>\nGo.",
            "</thinking>\n" + ANSWER + "\n</thinking>\n" + ANSWER,
            "</thinking>\n" + ANSWER + "\n<thinking>\nStill thinking",
            "<thinking>\n<thinking>\n</thinking>\n" + ANSWER,
            "Go.</thinking>\n<|DSML|calls>not an answer</|DSML|calls>\n" + ANSWER,
        ]
        for text in negatives:
            with self.subTest(text=text[:60]):
                self.assertEqual(self.recover(text), "")

    def test_real_answer_may_contain_fenced_tag_example(self):
        answer = ANSWER + '\n```xml\n</thinking>\n```'
        self.assertEqual(self.recover("Done analyzing.\n</thinking>\n" + answer), answer)

    def test_recovery_is_bounded_and_optional(self):
        self.assertEqual(self.recover("x" * 65537 + "\n</thinking>\n" + ANSWER), "")
        self.assertEqual(self.recover(MIXED, model="gpt-6-sol"), "")
        with mock.patch.dict(os.environ, {"WB_OPS_REASONING_RECOVERY": "off"}):
            self.assertEqual(self.recover(MIXED), "")
        recovery = wb.ReasoningAnswerRecovery(MODEL)
        recovery.feed(MIXED)
        recovery.block()
        self.assertEqual(recovery.answer(), "")
        self.assertEqual(recovery.parts, [])

    def test_responses_stream_recovers_once_without_retry(self):
        for size in (1, 17, 128, 4096):
            with self.subTest(size=size), mock.patch.object(wb, "open_upstream") as opened:
                frames = [chunk({"reasoning_content": MIXED[i:i + size]}) for i in range(0, len(MIXED), size)]
                stream = Stream(frames + turn())
                handler = self.response(stream, BODY)
                final = self.assert_lifecycle(handler)
                self.assertEqual(final["output_text"], ANSWER)
                self.assertEqual(sum(e["type"] == "response.output_text.delta" for e in responses(handler)), 1)
                self.assertEqual(final["usage"]["total_tokens"], 14)
                self.assertEqual(sum(o["type"] == "message" for o in final["output"]), 1)
                opened.assert_not_called()
                self.assertTrue(stream.closed)

    def test_chat_stream_recovers_once_before_finish(self):
        with mock.patch.object(wb, "open_upstream") as opened:
            handler = self.chat(Stream(turn({"reasoning_content": MIXED})), BODY)
        frames = responses(handler)
        content = [c["delta"]["content"] for f in frames for c in f.get("choices", []) if c["delta"].get("content")]
        self.assertEqual(content, [ANSWER])
        finish_frames = [f for f in frames
                         if any(c.get("finish_reason") for c in f.get("choices", []))]
        usage_frames = [f for f in frames if "usage" in f]
        self.assertEqual(len(finish_frames), 1)
        self.assertEqual(finish_frames[0]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(len(usage_frames), 1)
        self.assertEqual(usage_frames[0]["usage"]["total_tokens"], 14)
        self.assertEqual(b"".join(handler.written).count(b"[DONE]"), 1)
        opened.assert_not_called()

    def test_both_nonstream_apis_recover_without_retry(self):
        for api in ("chat", "responses"):
            with self.subTest(api=api), mock.patch.object(wb, "open_upstream") as opened:
                stream, handler = Stream(turn({"reasoning_content": MIXED})), Handler()
                if api == "chat":
                    handler._chat_nonstream_response(stream, MODEL, {"hash": "test"}, Account(), time.time(), base_body=BODY)
                    self.assertEqual(handler.json_body["choices"][0]["message"]["content"], ANSWER)
                else:
                    handler._responses_nonstream_response(stream, MODEL, set(), {}, {"hash": "test"}, Account(), time.time(), base_body=BODY)
                    self.assertEqual(handler.json_body["status"], "completed")
                    self.assertEqual(handler.json_body["output_text"], ANSWER)
                opened.assert_not_called()
                self.assertTrue(stream.closed)

    def test_normal_content_or_tools_win_even_on_terminal_frame(self):
        deltas = [{"content": "normal answer"},
                  {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "lookup", "arguments": "{}"}}]},
                  {"function_call": {"name": "lookup", "arguments": "{}"}}]
        for delta in deltas:
            with self.subTest(delta=delta):
                stream = Stream([chunk({"reasoning_content": MIXED}), chunk(delta, finish="stop"), b"data: [DONE]\n\n"])
                obj = wb.aggregate_stream(stream, MODEL, None)
                message = obj["choices"][0]["message"]
                self.assertNotIn(ANSWER, message.get("content") or "")

    def test_same_frame_boundary_and_finish_and_empty_placeholders(self):
        obj = wb.aggregate_stream(Stream([chunk({"reasoning_content": MIXED, "function_call": {"name": "", "arguments": ""},
                                                "tool_calls": []}, finish="stop"), b"data: [DONE]\n\n"]), MODEL, None)
        self.assertEqual(obj["choices"][0]["message"]["content"], ANSWER)

    def test_no_recovery_on_truncation_length_or_guard_failure(self):
        variants = [([chunk({"reasoning_content": MIXED})], "upstream_truncated"),
                    (turn({"reasoning_content": MIXED}, finish="length"), None),
                    ([chunk({"reasoning_content": MIXED}), b'data: {"error":{"code":"generation_timeout"}}\n\n'], "generation_timeout")]
        for frames, failure in variants:
            with self.subTest(failure=failure):
                obj = wb.aggregate_stream(Stream(frames), MODEL, None)
                self.assertEqual(obj["choices"][0]["message"].get("content") or "", "")
                self.assertEqual(obj.get("_ops_failure"), failure)

    def test_multiple_choices_disable_recovery(self):
        frame = {"choices": [{"index": 0, "delta": {"reasoning_content": MIXED}, "finish_reason": "stop"},
                             {"index": 1, "delta": {"reasoning_content": MIXED}, "finish_reason": "stop"}]}
        for frames, holder in [([b"data: " + json.dumps(frame).encode() + b"\n\n"], {}),
                               (turn({"reasoning_content": MIXED}), {"base_body": {"n": 2}})]:
            obj = wb.aggregate_stream(Stream(frames), MODEL, None, holder)
            self.assertEqual(obj["choices"][0]["message"].get("content") or "", "")

    def test_actual_reasoning_only_remains_failure_after_one_retry(self):
        with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn({"reasoning_content": "Still analyzing."})), Account("b"))) as opened:
            final = self.assert_lifecycle(self.response(Stream(turn({"reasoning_content": "Analyzing."})), BODY), "response.failed")
        self.assertEqual(final["error"]["code"], "reasoning_only_turn")
        self.assertEqual(opened.call_count, 1)

    def test_retry_can_recover_answer_with_one_terminal_event(self):
        with mock.patch.object(wb, "open_upstream", return_value=(Stream(turn({"reasoning_content": MIXED}, tokens=26)), Account("b"))) as opened:
            final = self.assert_lifecycle(self.response(Stream(turn({"reasoning_content": "Analyzing."})), BODY))
        self.assertEqual(final["output_text"], ANSWER)
        self.assertEqual(final["usage"]["total_tokens"], 40)
        self.assertEqual(opened.call_count, 1)

    def test_repair_does_not_bypass_repetition_guard(self):
        reasoning = "Analysis complete.\n</thinking>\n" + "Go write now.\n" * 200
        with mock.patch.object(wb, "open_upstream") as opened:
            final = self.assert_lifecycle(self.response(Stream(turn({"reasoning_content": reasoning})), BODY), "response.failed")
        self.assertEqual(final["error"]["code"], "reasoning_loop_detected")
        self.assertEqual(final["output_text"], "")
        opened.assert_not_called()

    def test_retry_transport_failures_have_specific_codes(self):
        for exc, code in [(TimeoutError("redacted"), "continuation_timeout"), (RuntimeError("redacted"), "continuation_request_failed")]:
            for api in ("chat", "responses"):
                with self.subTest(api=api, code=code), mock.patch.object(wb, "open_upstream", side_effect=exc) as opened:
                    stream = Stream(turn({"reasoning_content": "Still analyzing."}))
                    if api == "responses":
                        final = self.assert_lifecycle(self.response(stream, BODY), "response.failed")
                        self.assertEqual(final["error"]["code"], code)
                    else:
                        final = next(f for f in responses(self.chat(stream, BODY)) if "error" in f)
                        self.assertEqual(final["error"]["code"], code)
                    self.assertEqual(opened.call_count, 1)

    def test_failed_retry_probe_is_not_reasoning_only(self):
        first, second = Stream(turn({"reasoning_content": "Analyzing."})), Stream(turn())
        with mock.patch.object(wb, "open_upstream", return_value=(second, Account("b"))), \
                mock.patch.object(wb, "probe_first_frame", side_effect=[(first, Account()), (None, None)]):
            final = self.assert_lifecycle(self.response(first, BODY), "response.failed")
        self.assertEqual(final["error"]["code"], "continuation_probe_failed")
        self.assertTrue(second.closed)


if __name__ == "__main__":
    unittest.main()

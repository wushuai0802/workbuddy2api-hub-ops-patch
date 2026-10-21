"""Printed-call boundaries: closing-tag ownership and failure text.

The stream filter withholds text that may be a printed tool call. These cases
pin the two ways that must not go wrong: an unrelated closing tag in prose is
kept verbatim, and a call withheld by a failed turn is returned to the client
as the original text instead of being executed or dropped.
"""
import http.client
import json
import unittest

from test_integration import wb, Account, Stream, MODEL, BODY, chunk, turn

BAR = "\uff5c\uff5c"
INVOKE = ('<{b}DSML{b} invoke name="inspect">'
          '<{b}DSML{b} parameter name="path" string="true">a.txt'
          '</{b}DSML{b} parameter></{b}DSML{b} invoke>').format(b=BAR)
CALL = ('<{b}DSML{b} function_calls>' + INVOKE +
        '</{b}DSML{b} function_calls>').format(b=BAR)


def actions(text, size):
    """Run the filter over `text` in `size`-character deltas."""
    filt = wb.LeakedToolCallStreamFilter()
    out = []
    for i in range(0, len(text), size):
        out += filt.feed(text[i:i + size])
    out += filt.flush()
    return ("".join(value for kind, value in out if kind == "text"),
            [call for kind, value in out if kind == "calls" for call in value])


def collect(source):
    """Consume one Chat stream; return emitted (text, tool_calls) and any error."""
    holder = {"base_body": BODY, "account": Account("first")}
    frames, error = [], None
    try:
        for raw in wb._stream_chat_events(source, MODEL, holder):
            data = wb.strip_data_prefix(raw.decode())
            if not data or data == "[DONE]":
                continue
            frame = json.loads(data)
            for choice in frame.get("choices") or []:
                delta = choice.get("delta") or {}
                frames.append((delta.get("content") or "", delta.get("tool_calls")))
    except Exception as exc:  # transport errors are re-raised by design
        error = exc
    return frames, error


class PrintedCallBoundaryTests(unittest.TestCase):
    def test_isolated_closers_and_unrelated_tags_are_preserved(self):
        for text in ('</calls>', 'example: </invoke> end',
                     '```xml\n</function_calls>\n```',
                     'plain <tag>content</tag>'):
            for size in (1, 7, 65536):
                self.assertEqual(actions(text, size), (text, []), (text, size))

    def test_split_closing_tags_yield_one_call_and_no_markup(self):
        for size in (1, 17, len(CALL)):
            plain, calls = actions(CALL, size)
            self.assertEqual(plain, "", size)
            self.assertEqual(len(calls), 1, size)
            self.assertEqual(json.loads(calls[0]["arguments"]), {"path": "a.txt"})

    def test_hybrid_tail_has_a_scoped_budget(self):
        leaf = ('<run_code><parameter name="code">print(1)</parameter>'
                '<parameter name="description">hi</{b}DSML{b} parameter>'
                '</run_code>').format(b=BAR)
        trailer = ('</invoke></{b}DSML{b} parameter></{b}DSML{b} invoke>'
                   '</{b}DSML{b} calls>').format(b=BAR)
        for size in (1, 7, 65536):
            plain, calls = actions(leaf + trailer + "AFTER", size)
            self.assertEqual(plain, "AFTER", size)
            self.assertEqual(len(calls), 1, size)
            # A plain XML leaf has no evidence of the hybrid envelope.
            plain, calls = actions(
                leaf.replace('</{b}DSML{b} parameter>'.format(b=BAR),
                             "</parameter>") + "</invoke>", size)
            self.assertEqual(plain, "</invoke>", size)
            self.assertEqual(len(calls), 1, size)
            # Intervening prose ends ownership; later examples survive.
            plain, calls = actions(leaf + "example " + trailer, size)
            self.assertEqual(plain, "example " + trailer, size)
            self.assertEqual(len(calls), 1, size)

    def test_failed_printed_call_keeps_received_text_and_order(self):
        for finish in ("error", "content_filter", None):
            rows = [chunk({"content": value})
                    for value in ("BEFORE ", CALL, " AFTER")]
            if finish:
                rows += turn({}, finish)
            else:
                rows += [b"data: [DONE]\n", b"\n"]
            frames, error = collect(Stream(rows))
            self.assertIsNone(error, finish)
            self.assertFalse(any(calls for _text, calls in frames), finish)
            self.assertEqual("".join(text for text, _calls in frames),
                             "BEFORE " + CALL + " AFTER", finish)

    def test_disconnect_keeps_received_text_without_tool(self):
        rows = [chunk({"content": value})
                for value in ("BEFORE ", CALL, " AFTER")]
        frames, error = collect(
            Stream(rows, error=http.client.IncompleteRead(b"")))
        self.assertIsInstance(error, http.client.IncompleteRead)
        self.assertFalse(any(calls for _text, calls in frames))
        self.assertIn("BEFORE ", "".join(text for text, _calls in frames))

    def test_native_tools_take_precedence_over_printed_copy(self):
        rows = [chunk({"content": CALL}),
                *turn({"tool_calls": [{"index": 0, "id": "native",
                                       "function": {"name": "native_fn",
                                                    "arguments": "{}"}}]},
                      "tool_calls")]
        frames, error = collect(Stream(rows))
        self.assertIsNone(error)
        wire = json.dumps(frames)
        self.assertIn("native_fn", wire)
        self.assertNotIn('"name": "inspect"', wire)


if __name__ == "__main__":
    unittest.main()

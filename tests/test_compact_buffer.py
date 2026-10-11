"""Lossless representation, bounded storage, and tiny upstream SSE deltas."""
import json
import random
import unittest
from unittest import mock
from test_extra_fields import ExtraFieldsTests, MODES, WIRE
from test_integration import Account, Stream, chunk, turn, wb
from test_loop_recovery import GOOD_CALL, LOOP
from test_nullable_buffer import frames
from test_sse_framing import http_stream

NORMAL = ''.join(f'Fact {i}: evidence checked.\n' for i in range(18))

def tiny_frames(text, size=2):
    result = []
    for i in range(0, len(text), size):
        frame = json.loads(chunk(dict(WIRE, reasoning_content=text[i:i+size])).decode()[6:])
        frame['id'] = 'x' * 96
        result.append(('data: ' + json.dumps(frame) + '\n\n').encode())
    return result


def late_loop():
    guard = wb.ReasoningLoopGuard()
    for offset, ch in enumerate(LOOP, 1):
        if guard.feed(ch):
            break
    # Position this known synthetic loop between old byte and retained item
    # limits, to test storage policy rather than change detection thresholds.
    return 'Evidence ' + 'x' * (460-offset-11) + '.\n' + LOOP


class CompactBufferTests(ExtraFieldsTests):
    def append(self, buffer, raw):
        prepared = buffer.prepare(raw)
        self.assertTrue(buffer.fits(prepared))
        buffer.append_prepared(prepared)

    def test_roundtrip_preserves_exact_bytes_strings_and_boundaries(self):
        rows = [b'data: {"text":"hello"}\r\n', b'\r\n', 'data: {"text":"中文🚀"}\n',
                b'\n', b'\xff\xfe\x00', b'', '', b'prefix', b'prefix-tail', b'prefix',
                b'data: { "text" : "\\u4e2d" }\n\n']
        buffer = wb.CompactSSEBuffer()
        for raw in rows:
            self.append(buffer, raw)
        self.assertEqual(list(buffer), rows)
        self.assertEqual([type(x) for x in buffer], [type(x) for x in rows])
        self.assertEqual(len(buffer), len(rows))
        self.assertLessEqual(buffer.storage_bytes, 65536)
        buffer.clear()
        self.assertEqual(len(buffer), 0)
        self.assertEqual(buffer.storage_bytes, 0)
        self.assertIsNone(buffer.base)
        self.append(buffer, 'new segment')
        self.assertEqual(list(buffer), ['new segment'])

    def test_seeded_roundtrip_for_common_prefix_suffix_and_unrelated_items(self):
        rng = random.Random(16101005)
        buffer = wb.CompactSSEBuffer()
        expected = []
        for _ in range(400):
            raw = bytes(rng.randrange(256) for _ in range(rng.randrange(100)))
            if rng.randrange(2):
                raw = b'data: {"id":"stable","reasoning":"' + raw + b'"}\n\n'
            candidate = buffer.prepare(raw)
            if not buffer.fits(candidate):
                self.assertEqual(list(buffer), expected)
                buffer.clear();expected = []
                candidate = buffer.prepare(raw)
            buffer.append_prepared(candidate);expected.append(raw)
            self.assertLessEqual(buffer.storage_bytes, 65536)
        self.assertEqual(list(buffer), expected)

    def test_single_item_storage_and_item_count_are_hard_bounds(self):
        buffer = wb.CompactSSEBuffer()
        self.assertIsNone(buffer.prepare(b'x' * 65537))
        with self.assertRaises(BufferError):
            buffer.append_prepared(buffer.prepare(b'x' * 65536))
        self.assertEqual(len(buffer), 0)
        for _ in range(512):
            self.append(buffer, b'\n')
        with self.assertRaises(BufferError):
            buffer.append_prepared(buffer.prepare(b'\n'))
        self.assertEqual(len(buffer), 512)
        self.assertEqual(list(buffer), [b'\n'] * 512)

    def test_uncompressible_input_refuses_without_mutating_buffer(self):
        rng = random.Random(103)
        rows = []
        buffer = wb.CompactSSEBuffer()
        for _ in range(40):
            data = bytes(rng.randrange(256) for _ in range(4096))
            candidate = buffer.prepare(data)
            if not buffer.fits(candidate):
                before = buffer.storage_bytes
                with self.assertRaises(BufferError):
                    buffer.append_prepared(candidate)
                self.assertEqual(buffer.storage_bytes, before)
                break
            buffer.append_prepared(candidate);rows.append(data)
        self.assertGreater(len(rows), 0)
        self.assertEqual(list(buffer), rows)

    def test_real_line_iteration_holds_more_than_64k_raw_without_rewriting(self):
        data = tiny_frames(NORMAL) + turn({'content': 'answer'})
        for transport in (1, 17, 4096):
            self.clear_events()
            source = http_stream(data, transport)
            expected = list(source)
            guard = wb._GuardedStream(http_stream(data, transport), {'model': 'deepseek-v4.1-flash', 'generation_deadline': None})
            try:
                with mock.patch.object(wb.time, 'monotonic', return_value=0):
                    output = list(guard)
            finally:
                guard.close()
            self.assertEqual(output, expected)
            event = next(e for e in wb.loop_health_snapshot()['recent'] if e['kind'] == 'reasoning_buffer_released')
            self.assertGreater(event['buffered_bytes'], 65536)
            self.assertLess(event['buffered_storage_bytes'], 20000)
            self.assertEqual(event['buffer_reason'], 'content')

    def test_existing_item_limit_still_releases_tiny_frames(self):
        data = tiny_frames(NORMAL * 3) + turn({'content': 'answer'})
        guard = wb._GuardedStream(http_stream(data), {'model': 'deepseek-v4.1-flash', 'generation_deadline': None})
        try:
            with mock.patch.object(wb.time, 'monotonic', return_value=0):
                output = list(guard)
        finally:
            guard.close()
        self.assertEqual(output, list(http_stream(data)))
        event = next(e for e in wb.loop_health_snapshot()['recent'] if e.get('buffer_reason') == 'frames')
        self.assertEqual(event['buffered_items'], 1024)
        self.assertEqual(event['buffered_records'], 512)
        self.assertLessEqual(event['buffered_storage_bytes'], 65536)

    def test_known_loop_in_tiny_frames_is_withheld_and_recovers_all_modes(self):
        for api in MODES:
            for size in (1, 17, 4096):
                self.clear_events()
                with mock.patch.object(wb.time, 'monotonic', return_value=0), mock.patch.object(wb, 'open_upstream', return_value=(http_stream(turn({'content':'answer'}), size), Account('b'))) as opened:
                    h = self.invoke(api, http_stream(tiny_frames(late_loop()), size))
                self.assertEqual(opened.call_count, 1)
                self.assertIn('answer', self.output(h))
                self.assertNotIn('Let me run', self.output(h))
                detail = next(e for e in wb.loop_health_snapshot()['recent'] if e['kind'] == 'reasoning_buffer_discarded')
                self.assertGreater(detail['buffered_bytes'], 65536)
                self.assertLessEqual(detail['buffered_storage_bytes'], 65536)
                self.assertEqual(self.counts().get('retry_recovered'), 1)

    def test_two_loops_remain_one_retry_and_real_tools_block_retry(self):
        for api in MODES:
            with mock.patch.object(wb.time, 'monotonic', return_value=0), mock.patch.object(wb, 'open_upstream', return_value=(http_stream(tiny_frames(late_loop())), Account('b'))) as opened:
                h = self.invoke(api, http_stream(tiny_frames(late_loop())))
            self.assertEqual(opened.call_count, 1)
            self.assertIn('reasoning_loop_detected', self.output(h))
            with mock.patch.object(wb, 'open_upstream') as opened:
                self.invoke(api, http_stream([chunk(GOOD_CALL)] + tiny_frames(late_loop())))
            opened.assert_not_called()

    def test_successful_tools_content_usage_and_unknown_boundaries_survive(self):
        for api in MODES:
            for final, finish in (({'content': 'answer'}, 'stop'), (GOOD_CALL, 'tool_calls')):
                data = tiny_frames(NORMAL) + turn(final, finish)
                with mock.patch.object(wb, 'open_upstream') as opened:
                    h = self.invoke(api, http_stream(data))
                opened.assert_not_called()
                self.assertEqual(self.thought(h), NORMAL)
                self.assertIn('answer' if finish == 'stop' else 'lookup', self.output(h))
        for boundary in (chunk(dict(WIRE, extra_fields={'unknown': 'private'})),
                         chunk(usage={'completion_tokens': 1}), b'data: {bad\n'):
            data = tiny_frames('Evidence.') + [boundary]
            self.assertEqual(self.raw(data), data)

    def test_incompressible_cap_heartbeat_time_transport_and_eof(self):
        frame = json.loads(chunk(dict(WIRE, reasoning_content='Evidence.')).decode()[6:])
        frame['id'] = 'x' * 40000
        raw = ('data: ' + json.dumps(frame) + '\n\n').encode()
        self.assertEqual(self.raw([raw, raw]), [raw, raw])
        self.assertGreater(self.counts().get('reasoning_buffer_release_bytes', 0), 0)
        data = tiny_frames('Evidence.')
        guard = wb._GuardedStream(Stream(data, error=OSError('disconnect')), {'model': 'deepseek-v4.1-flash', 'generation_deadline': None})
        out = []
        try:
            with self.assertRaises(OSError):
                for line in guard:
                    out.append(line)
        finally:
            guard.close()
        self.assertEqual(out, data)
        self.clear_events()
        with mock.patch.object(wb.time, 'monotonic', side_effect=[0, 3]):
            self.assertEqual(self.raw([data[0], b': heartbeat\n']), [data[0], b': heartbeat\n'])
        self.assertEqual(self.counts().get('reasoning_buffer_release_time'), 1)


if __name__ == '__main__':
    suite = unittest.TestSuite(CompactBufferTests(n) for n in CompactBufferTests.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

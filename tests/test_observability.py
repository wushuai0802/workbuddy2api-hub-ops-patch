"""Observation-only signals, byte-cap diagnostics, and unchanged API behavior."""
import json
import os
import unittest
from unittest import mock
from test_extra_fields import ExtraFieldsTests, MODES, WIRE
from test_nullable_buffer import frames
from test_integration import Account, Stream, chunk, turn, wb
from test_loop_recovery import GOOD_CALL, LOOP
from test_sse_framing import http_stream


MIXED = ("Go.\nLet me run.\n" +
         "The available evidence supports checking the dependency before choosing an operation; "
         "the next step should preserve existing results and verify the outstanding condition.\n") * 14


class ObservationTests(ExtraFieldsTests):
    def observe(self, text, size=128):
        observer = wb.StallObserver()
        results = []
        for i in range(0, len(text), size):
            event = observer.feed(text[i:i+size])
            if event:
                results.append(event)
        event = observer.feed("", final=True)
        if event:
            results.append(event)
        return observer, results

    def test_mixed_prose_observation_is_partition_invariant_and_once(self):
        observations = []
        for size in (1, 17, 128, 1024):
            observer, events = self.observe(MIXED * 3, size)
            self.assertEqual(len(events), 1)
            self.assertTrue(observer.reported)
            self.assertEqual(len(observer.window), 0)
            observations.append(events[0])
        self.assertTrue(all(e == observations[0] for e in observations))
        self.assertGreaterEqual(observations[0]['intent_lines'], 12)
        guard = wb.ReasoningLoopGuard()
        self.assertFalse(guard.feed(MIXED))

    def test_negatives_code_quotes_math_tools_and_sparse_planning(self):
        negatives = [
            '```python\n' + MIXED + '\n```',
            '~~~\n' + MIXED + '\n~~~',
            '\n'.join('> ' + line for line in MIXED.splitlines()),
            "print('Go. Let me run.')\n" * 50,
            'a(n) = n**2 + 1\n' * 50,
            '| Go | Let me run |\n' * 50,
            'Let me run a separate validation for this hypothesis.\n' * 30,
            'Go.\nLet me run.\n' * 3,
            ('Go.\nLet me run.\n' + 'New evidence resolves a different requirement.\n' * 16) * 20,
        ]
        for text in negatives:
            self.assertEqual(self.observe(text)[1], [], text[:40])

    def test_observer_memory_bounds_and_window_expiry(self):
        observer = wb.StallObserver()
        observer.feed('x' * 100000)
        self.assertLessEqual(len(observer.line), 2048)
        self.assertLessEqual(observer.line_chars, 8193)
        self.assertIsNone(observer.feed('\n'))
        self.assertEqual(len(observer.window), 0)
        observer.feed('Go.\nLet me run.\n' * 3)
        observer.feed('Distinct evidence without procedural announcements.\n' * 200)
        self.assertLessEqual(len(observer.window), 96)
        self.assertLessEqual(sum(n for _, n in observer.window), 8192)
        self.assertIsNone(observer.feed('Go.\nLet me run.\n' * 3, final=True))

    def test_final_line_and_separate_attempts(self):
        text = MIXED[:MIXED.index('Go.', 1000)] if len(MIXED) > 1000 else MIXED
        for value in (text, MIXED.rstrip('\n')):
            expected = self.observe(value, 1)[1]
            self.assertEqual(self.observe(value, 1024)[1], expected)
        self.assertEqual(len(self.observe(MIXED)[1]), 1)
        self.assertEqual(len(self.observe(MIXED)[1]), 1)

    def test_mixed_signal_does_not_change_wire_or_holder(self):
        data = frames(WIRE, 128, MIXED) + turn(GOOD_CALL, 'tool_calls')
        outputs = []
        for enabled in ('0', '1'):
            self.clear_events()
            holder = {'model': 'deepseek-v4.1-flash', 'generation_deadline': None}
            with mock.patch.dict(os.environ, {'WB_OPS_STALL_OBSERVE': enabled}), mock.patch.object(wb.time, 'monotonic', return_value=0):
                outputs.append(self.raw(data, holder))
            self.assertNotIn('loop_evidence', holder)
            self.assertEqual(self.counts().get('reasoning_stall_observed', 0), int(enabled))
            self.assertNotIn('reasoning_loop_detected', self.counts())
        self.assertEqual(outputs[0], data)
        self.assertEqual(outputs[1], data)

    def test_all_apis_keep_answer_tools_usage_and_no_added_retry(self):
        for api in MODES:
            for last, finish in (({'content': 'answer'}, 'stop'), (GOOD_CALL, 'tool_calls')):
                outcomes = []
                for enabled in ('0', '1'):
                    self.clear_events()
                    with mock.patch.dict(os.environ, {'WB_OPS_STALL_OBSERVE': enabled}), mock.patch.object(wb, 'open_upstream') as opened:
                        handler = self.invoke(api, http_stream(frames(WIRE, 128, MIXED) + turn(last, finish), 7))
                    opened.assert_not_called()
                    self.assertIn('answer' if finish == 'stop' else 'lookup', self.output(handler))
                    outcomes.append(self.thought(handler))
                    self.assertEqual(self.counts().get('reasoning_stall_observed', 0), int(enabled))
                self.assertEqual(outcomes[0], MIXED)
                self.assertEqual(outcomes[0], outcomes[1])

    def test_content_arguments_and_non_deepseek_are_not_observed(self):
        cases = [([chunk({'content': MIXED})], 'deepseek-v4.1-flash'),
                 ([chunk({'tool_calls': [{'index': 0, 'function': {'arguments': MIXED}}]})], 'deepseek-v4.1-flash'),
                 (frames(WIRE, 128, MIXED), 'another-model')]
        for data, model in cases:
            self.clear_events()
            self.raw(data + turn(), {'model': model, 'generation_deadline': None})
            self.assertNotIn('reasoning_stall_observed', self.counts())

    def test_buffer_metrics_count_utf8_items_frames_duration_and_trigger(self):
        data = [chunk(dict(WIRE, reasoning_content='中文 evidence.')), b'\n']
        boundary = chunk({'content': 'answer'})
        with mock.patch.object(wb.time, 'perf_counter', side_effect=[10, 10.25]):
            self.assertEqual(self.raw(data + [boundary]), data + [boundary])
        event = next(e for e in wb.loop_health_snapshot()['recent'] if e['kind'] == 'reasoning_buffer_released')
        self.assertEqual(event['buffered_chars'], len('中文 evidence.'))
        self.assertEqual(event['buffered_bytes'], sum(map(len, data)))
        self.assertEqual(event['buffered_items'], 2)
        self.assertEqual(event['buffered_frames'], 1)
        self.assertEqual(event['buffered_ms'], 250)
        self.assertEqual(event['trigger_bytes'], len(boundary))
        self.assertEqual(event['trigger_chars'], 0)
        self.assertEqual(event['buffer_reason'], 'content')

    def test_protocol_overhead_byte_cap_is_measured_not_relaxed(self):
        event = json.loads(chunk(dict(WIRE, reasoning_content='Go.')).decode()[6:])
        event['id'] = 'PRIVATE_' + 'x' * 40000
        first = ('data: ' + json.dumps(event) + '\n').encode()
        data = [first, b'\n', first]
        with mock.patch.object(wb.time, 'monotonic', return_value=0):
            self.assertEqual(self.raw(data), data)
        detail = next(e for e in wb.loop_health_snapshot()['recent'] if e.get('buffer_reason') == 'bytes')
        self.assertEqual(detail['buffered_bytes'], len(first) + 1)
        self.assertLessEqual(detail['buffered_bytes'], 65536)
        self.assertEqual(detail['trigger_bytes'], len(first))
        self.assertEqual(detail['buffered_chars'], 3)
        self.assertEqual(detail['trigger_chars'], 3)
        self.assertNotIn('PRIVATE_', json.dumps(wb.loop_health_snapshot()))

    def test_metrics_eof_transport_and_loop_discard(self):
        for error, reason in ((None, 'eof'), (OSError('disconnect'), 'transport_error')):
            self.clear_events()
            data = [chunk(dict(WIRE, reasoning_content='Evidence.')), b'\n']
            guard = wb._GuardedStream(Stream(data, error=error), {'model': 'deepseek-v4.1-flash', 'generation_deadline': None})
            try:
                if error:
                    with self.assertRaises(OSError):
                        list(guard)
                else:
                    self.assertEqual(list(guard), data)
            finally:
                guard.close()
            detail = next(e for e in wb.loop_health_snapshot()['recent'] if e.get('buffer_reason') == reason)
            self.assertEqual(detail['buffered_bytes'], sum(map(len, data)))
            self.assertEqual(detail['buffered_frames'], 1)
        self.clear_events()
        with mock.patch.object(wb.time, 'monotonic', return_value=0):
            self.raw(frames(WIRE, 128, LOOP))
        detail = next(e for e in wb.loop_health_snapshot()['recent'] if e['kind'] == 'reasoning_buffer_discarded')
        self.assertEqual(detail['buffer_reason'], 'loop')
        self.assertGreater(detail['buffered_bytes'], 0)

    def test_privacy_and_telemetry_schema_are_bounded(self):
        wb._loop_event('reasoning_buffer_released', metrics={'buffered_bytes': -1, 'buffered_chars': 'SECRET',
                       'buffered_frames': True, 'trigger_bytes': 10**30, 'prompt': 'SECRET'}, reason='SECRET')
        event = wb.loop_health_snapshot()['recent'][-1]
        self.assertNotIn('SECRET', json.dumps(event))
        self.assertEqual(event['buffered_bytes'], 0)
        self.assertNotIn('buffered_frames', event)
        self.assertEqual(event['trigger_bytes'], 9007199254740991)
        for _ in range(100):
            wb._loop_event('reasoning_stall_observed', metrics={'intent_lines': 12})
        self.assertLessEqual(len(wb.loop_health_snapshot()['recent']), 64)

    def test_no_extra_loop_retry_when_observation_is_enabled(self):
        for api in MODES:
            self.clear_events()
            with mock.patch.object(wb, 'open_upstream', return_value=(Stream(frames(WIRE, 128)), Account('b'))) as opened:
                handler = self.invoke(api, Stream(frames(WIRE, 128)))
            self.assertEqual(opened.call_count, 1)
            self.assertIn('reasoning_loop_detected', self.output(handler))
            self.assertEqual(self.counts().get('retry_failed'), 1)


if __name__ == '__main__':
    suite = unittest.TestSuite(ObservationTests(n) for n in ObservationTests.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

"""Regression tests for quoted examples, consumed usage, diagnostics and SSE caps."""
import io
import json
import os
from types import SimpleNamespace
import unittest
from unittest import mock

from test_integration import Account, BODY, Handler, MODEL, Stream, chunk, responses, turn, wb
from test_guarded_sse_batching import ChunkSource


REPEAT = 'Let me write the actual tool call.\n' * 80
USAGE = {'prompt_tokens': 71, 'completion_tokens': 22, 'total_tokens': 93,
         'prompt_tokens_details': {'cached_tokens': 17},
         'completion_tokens_details': {'reasoning_tokens': 11}}


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'WB_OPS_LOOP_RETRY': '0',
                          'WB_OPS_GENERATION_TIMEOUT': '30', 'WB_OPS_OBSERVE': '0'}))
        self.enterContext(mock.patch.object(wb, 'retry_bump'))
        self.enterContext(mock.patch.object(wb, 'apply_daily_token_limit'))

    def invoke(self, protocol, stream, source, writer_error=None):
        handler = Handler('/v1/' + {'chat': 'chat/completions', 'responses': 'responses',
                                   'messages': 'messages'}[protocol], writer_error=writer_error)
        args = [source, MODEL]
        if protocol == 'responses':
            args.extend([set(), {}])
        args.extend([{}, Account(), wb.time.time()])
        records = []
        with mock.patch.object(wb, 'record_usage', side_effect=lambda *a, **k: records.append(
                ('usage', a[1] if len(a) > 1 else k.get('usage'), k))), \
             mock.patch.object(wb, 'record_error', side_effect=lambda *a, **k: records.append(
                ('error', k.get('usage'), k))), \
             mock.patch.object(wb, 'open_upstream', side_effect=AssertionError('unexpected replay')):
            getattr(handler, '_' + protocol + ('_stream_response' if stream else '_nonstream_response'))(
                *args, base_body=BODY)
        self.assertTrue(source.closed)
        return handler, records

    def test_quoted_examples_survive_all_six_paths(self):
        for quote, close in [('"', '"'), ('“', '”'), ("'", "'"), ('‘', '’')]:
            quoted = 'A verbatim diagnostic sample follows:\n' + quote + '\n' + REPEAT + close + '\nEnd of sample.'
            for field in ('reasoning_content', 'content'):
                for protocol in ('chat', 'responses', 'messages'):
                    for stream in (True, False):
                        with self.subTest(quote=quote, field=field, protocol=protocol, stream=stream):
                            frames = [chunk({field: quoted[i:i+17]}) for i in range(0, len(quoted), 17)]
                            if field == 'reasoning_content':
                                frames += [chunk({'content': 'The quoted sample is preserved.'})]
                            handler, _ = self.invoke(protocol, stream, Stream(frames + turn()))
                            value = json.dumps(responses(handler) if stream else handler.json_body)
                            self.assertNotIn('loop_detected', value)
                            self.assertIn('End of sample.', value)

    def test_quote_boundary_partition_and_real_loop_after_close(self):
        quoted = '"\n' + REPEAT + '"\n'
        for field in ('reasoning_content', 'content'):
            for size in (1, 17, 64, len(quoted)):
                with self.subTest(field=field, size=size):
                    source = Stream([chunk({field: quoted[i:i+size]}) for i in range(0, len(quoted), size)])
                    guarded = wb._GuardedStream(source, {'model': MODEL, 'base_body': BODY})
                    try:
                        self.assertNotIn(b'loop_detected', b''.join(guarded))
                    finally:
                        guarded.close()
                    actual = quoted + REPEAT
                    source = Stream([chunk({field: actual[i:i+size]}) for i in range(0, len(actual), size)])
                    guarded = wb._GuardedStream(source, {'model': MODEL, 'base_body': BODY})
                    try:
                        self.assertIn(b'loop_detected', b''.join(guarded))
                    finally:
                        guarded.close()

    def test_escaped_quotes_and_apostrophes_do_not_leak_protection(self):
        for text in ['"\nAn escaped \\" marker.\n' + REPEAT + '"\n',
                     '“\nAn escaped \\” marker.\n' + REPEAT + '”\n']:
            guard = wb.ReasoningLoopGuard()
            self.assertFalse(guard.feed(text))
        for prefix in ["I'll inspect the tools' results.\n", "The clients' state is unchanged.\n"]:
            guard = wb.ReasoningLoopGuard()
            self.assertTrue(guard.feed(prefix + REPEAT))

    def test_unclosed_quote_is_bounded(self):
        guard = wb.ReasoningLoopGuard()
        guard.feed('"\n' + 'Ordinary quoted context.\n' * 4000)
        self.assertTrue(guard.feed(REPEAT))

    def test_long_quoted_line_closes_before_a_real_loop(self):
        quoted = '"' + 'Let me write. '*600 + '"\n'
        guard = wb.ReasoningLoopGuard()
        self.assertFalse(guard.feed(quoted))
        self.assertTrue(guard.feed(REPEAT))

    def test_leading_thinking_preserves_quoted_sample(self):
        text = '<thinking>\n"\n' + REPEAT + '"\n</thinking>\nThe diagnosis is complete.'
        for size in (1, 17, 64, len(text)):
            guard = wb.LeakedThinkingGuard(mixed_stall=wb.MixedStallGuard())
            for i in range(0, len(text), size):
                self.assertFalse(guard.feed(text[i:i+size]))
            self.assertFalse(guard.finish())
            self.assertTrue(guard.detected)

    def test_structured_sentence_does_not_exempt_surrounding_mixed_stall(self):
        text = ('Hmm. Hmm. Let me go. The call f(x) is still unresolved.\n') * 80
        holder = {'model': MODEL, 'base_body': BODY}
        guard = wb._GuardedStream(Stream([chunk({'reasoning_content': text})]), holder)
        try:
            self.assertIn(b'reasoning_loop_detected', b''.join(guard))
        finally:
            guard.close()
        self.assertEqual(holder['loop_evidence']['rule'], 'mixed-stall')

    def test_prefetched_usage_survives_reasoning_and_content_failure_six_paths(self):
        for field in ('reasoning_content', 'content'):
            for protocol in ('chat', 'responses', 'messages'):
                for stream in (True, False):
                    with self.subTest(field=field, protocol=protocol, stream=stream):
                        source = ChunkSource([chunk({field: REPEAT}) + chunk(finish='stop', usage=USAGE)
                                              + b'data: [DONE]\n\n'])
                        _, records = self.invoke(protocol, stream, source)
                        self.assertEqual([u for _, u, _ in records], [USAGE])
                        self.assertEqual(source.reads, 1)

    def test_prefetched_usage_survives_client_disconnect(self):
        for protocol in ('chat', 'responses', 'messages'):
            with self.subTest(protocol=protocol):
                source = ChunkSource([b''.join(turn({'content': 'A concrete result.'})) + chunk(usage=USAGE)])
                _, records = self.invoke(protocol, True, source, writer_error=BrokenPipeError())
                self.assertEqual([u for _, u, _ in records], [USAGE])
                self.assertEqual(source.reads, 1)
                self.assertEqual(records[-1][2]['outcome'], 'client_aborted')

    def test_prefetched_usage_survives_abnormal_candidate_six_paths(self):
        bad = ('data: '+json.dumps({'choices':[{'index':1,'delta':{'content':'unsupported'}}]})+'\n\n').encode()
        wire = chunk({'role':'assistant'})+bad+chunk(usage=USAGE)+b'data: [DONE]\n\n'
        for protocol in ('chat', 'responses', 'messages'):
            for stream in (True, False):
                with self.subTest(protocol=protocol, stream=stream):
                    source = ChunkSource([wire])
                    handler, records = self.invoke(protocol, stream, source)
                    self.assertEqual([u for _,u,_ in records], [USAGE])
                    self.assertIn('upstream_choices_unsupported', json.dumps(responses(handler) if stream else handler.json_body))
                    self.assertEqual(source.reads, 1)

    def test_prefetched_usage_survives_transport_failure_six_paths(self):
        class BrokenSource(ChunkSource):
            def read1(self, size):
                if self.reads >= 1:
                    self.reads += 1
                    raise OSError('synthetic transport reset')
                return super().read1(size)
        for protocol in ('chat', 'responses', 'messages'):
            for stream in (True, False):
                with self.subTest(protocol=protocol, stream=stream):
                    source = BrokenSource([chunk({'content':'partial'})+chunk(usage=USAGE)])
                    _, records = self.invoke(protocol, stream, source)
                    self.assertEqual([u for _,u,_ in records], [USAGE])
                    self.assertEqual(source.reads, 2)

    def test_deadline_keeps_usage_received_in_same_read(self):
        source = ChunkSource([chunk({'content':'partial'})+chunk(usage=USAGE)])
        guard = wb._GuardedStream(source, {'model':MODEL,'base_body':BODY})
        source.before_read = lambda count: guard.expired.set()
        try:
            frames = [json.loads(line[6:]) for raw in guard for line in raw.decode().splitlines() if line.startswith('data: {')]
        finally:
            guard.close()
        self.assertIn(USAGE, [f['usage'] for f in frames if f.get('usage')])
        self.assertTrue(any(f.get('error',{}).get('code')=='generation_timeout' for f in frames))
        self.assertEqual(source.reads, 1)

    def test_deadline_limited_socket_timeout_is_not_an_idle_transport_error(self):
        class EarlyTimeout(ChunkSource):
            def __init__(self):
                super().__init__([])
                self.fp = SimpleNamespace(_sock=mock.Mock())
            def read1(self, size):
                raise TimeoutError('socket timer fired before monotonic tick')
        for budget, deadline_error in [(1, True), (300, False)]:
            source = EarlyTimeout()
            with mock.patch.object(wb, 'upstream_timeouts', return_value=(30, 120)):
                guard = wb._GuardedStream(source, {'model': MODEL, 'base_body': BODY,
                                          'generation_deadline': wb.time.monotonic()+budget})
            try:
                if deadline_error:
                    self.assertIn(b'generation_timeout', b''.join(guard))
                else:
                    with self.assertRaises(TimeoutError):
                        list(guard)
            finally:
                guard.close()

    def test_sse_limit_failure_has_one_error_terminal_and_keeps_consumed_usage(self):
        wire = chunk({'role':'assistant'})+chunk(usage=USAGE)+b'data: '+b'x'*200000
        for protocol in ('chat', 'responses', 'messages'):
            for stream in (True, False):
                with self.subTest(protocol=protocol, stream=stream):
                    source = ChunkSource([wire])
                    with mock.patch.dict(os.environ, {'WB_OPS_SSE_MAX_LINE_BYTES':'131072'}):
                        handler, records = self.invoke(protocol, stream, source)
                    self.assertEqual([u for _,u,_ in records], [USAGE])
                    self.assertIn('upstream_sse_limit', json.dumps(responses(handler) if stream else handler.json_body))
                    self.assertLessEqual(source.reads, 3)

    def test_sse_line_cap_stops_before_unbounded_read(self):
        source = ChunkSource([b'data: ' + b'x' * 200000])
        with mock.patch.dict(os.environ, {'WB_OPS_SSE_MAX_LINE_BYTES': '131072'}):
            with self.assertRaisesRegex(Exception, 'upstream_sse_limit'):
                list(wb.iter_sse_lines(source))
        self.assertLessEqual(source.reads, 3)

    def test_sse_cap_preserves_large_valid_frames_utf8_and_crlf(self):
        frame = chunk({'tool_calls': [{'index': 0, 'id': 'c1', 'function': {
            'name': 'lookup', 'arguments': json.dumps({'value': '中文' * 20000})}}]}).replace(b'\n', b'\r\n')
        source = ChunkSource([frame[i:i+127] for i in range(0, len(frame), 127)])
        with mock.patch.dict(os.environ, {'WB_OPS_SSE_MAX_LINE_BYTES': '1048576'}):
            self.assertEqual(b''.join(wb.iter_sse_lines(source)), frame)

    def test_sse_cap_applies_to_iterable_sources(self):
        with mock.patch.dict(os.environ, {'WB_OPS_SSE_MAX_LINE_BYTES': '1024'}):
            with self.assertRaisesRegex(Exception, 'upstream_sse_limit'):
                list(wb.iter_sse_lines(Stream([b'data: ' + b'x' * 4096 + b'\n'])))

    def test_sse_event_cap_and_normal_separator_reset(self):
        line = b':'+b'x'*511+b'\n'
        with mock.patch.dict(os.environ, {'WB_OPS_SSE_MAX_EVENT_BYTES':'1024'}):
            with self.assertRaisesRegex(Exception, 'upstream_sse_limit'):
                list(wb.iter_sse_lines(ChunkSource([line*4])))
            wire = (line+b'\n')*8
            self.assertEqual(b''.join(wb.iter_sse_lines(ChunkSource([wire]))), wire)

    def test_health_separates_terminal_outcomes_from_token_heuristics(self):
        rows = [{'at': 1, 'model': MODEL, 'outcome': 'failed', 'completion_tokens': 0},
                {'at': 2, 'model': MODEL, 'outcome': 'client_aborted', 'completion_tokens': 5, 'reasoning_tokens': 5},
                {'at': 3, 'model': MODEL, 'outcome': 'completed', 'completion_tokens': 4, 'reasoning_tokens': 4}]
        def synthetic_open(path, *args, **kwargs):
            if str(path) == str(wb.USAGE_LOG):
                return io.StringIO(''.join(json.dumps(r)+'\n' for r in rows))
            raise FileNotFoundError(path)
        with mock.patch('builtins.open', side_effect=synthetic_open):
            health = wb.retry_health_snapshot(10)
        self.assertEqual(health['failed'], 1)
        self.assertEqual(health['client_aborted'], 1)
        self.assertEqual(health['turns'], 1)
        self.assertIn('legacy_window', health)
        self.assertEqual(health['legacy_window']['scope'], 'last_usage_rows')


if __name__ == '__main__':
    unittest.main()

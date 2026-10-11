"""Separated framing limits, bounded observation buffering, detect-only content."""
import json
import os
import unittest
from unittest import mock
from test_compact_buffer import NORMAL, tiny_frames
from test_observability import MIXED
from test_extra_fields import ExtraFieldsTests, MODES, WIRE
from test_nullable_buffer import frames
from test_integration import Account, Stream, chunk, turn, wb
from test_loop_recovery import GOOD_CALL, LOOP
from test_sse_framing import http_stream


def delayed_loop():
    guard = wb.ReasoningLoopGuard()
    for offset, ch in enumerate(LOOP, 1):
        if guard.feed(ch):
            break
    return 'Evidence ' + 'x' * (900-offset-11) + '.\n' + LOOP


class RecordTests(ExtraFieldsTests):
    def test_attached_separators_preserve_types_bytes_order_and_storage(self):
        b = wb.CompactSSEBuffer()
        expected = []
        for raw, sep in ((b'data: abc\n', '\r\n'), ('data: 中文\r\n', b'\n'), (b'\xff', b'\r\n')):
            b.append_prepared(b.prepare(raw));before = b.storage_bytes
            b.append_prepared(b.prepare(sep, attach_separator=True))
            self.assertEqual(b.storage_bytes, before)
            expected.extend((raw, sep))
        self.assertEqual(list(b), expected)
        self.assertEqual([type(x) for x in b], [type(x) for x in expected])
        self.assertEqual((len(b), b.record_count), (6, 3))
        b.clear();self.assertEqual((len(b), b.record_count, b.storage_bytes), (0, 0, 0))

    def test_no_unbounded_separator_runs_or_whitespace_normalization(self):
        b = wb.CompactSSEBuffer();expected = [b'data: test\n', b'\n', b'\n', b' \t\r\n', '', b'\r\n']
        for raw in expected:b.append_prepared(b.prepare(raw, attach_separator=True))
        self.assertEqual(list(b), expected)
        self.assertGreater(b.record_count, 2)
        b.clear()
        for _ in range(512):
            b.append_prepared(b.prepare(b'data: x\n'))
            b.append_prepared(b.prepare(b'\n', attach_separator=True))
        self.assertEqual((len(b), b.record_count), (1024, 512))
        before = b.storage_bytes
        with self.assertRaises(BufferError):b.append_prepared(b.prepare(b'\n', attach_separator=True))
        self.assertEqual((len(b), b.storage_bytes), (1024, before))

    def test_real_line_transport_uses_records_and_remains_byte_exact(self):
        data = tiny_frames(NORMAL*2) + turn(GOOD_CALL, 'tool_calls')
        for size in (1, 17, 4096):
            self.clear_events()
            with mock.patch.object(wb.time, 'monotonic', return_value=0):
                output = self.raw(http_stream(data, size))
            self.assertEqual(output, list(http_stream(data, size)))
            event = next(e for e in wb.loop_health_snapshot()['recent'] if e['kind']=='reasoning_buffer_released')
            self.assertEqual(event['buffer_reason'], 'tool')
            self.assertGreater(event['buffered_items'], 512)
            self.assertLessEqual(event['buffered_records'], 512)
            self.assertLessEqual(event['buffered_storage_bytes'], 65536)

    def test_loop_after_old_item_limit_is_isolated_in_all_apis(self):
        for api in MODES:
            self.clear_events()
            with mock.patch.object(wb.time, 'monotonic', return_value=0), mock.patch.object(wb, 'open_upstream', return_value=(Stream(turn({'content':'answer'})), Account('b'))) as opened:
                h = self.invoke(api, http_stream(tiny_frames(delayed_loop()), 17))
            self.assertEqual(opened.call_count, 1)
            self.assertNotIn('Evidence', self.output(h))
            self.assertIn('answer', self.output(h))
            ev = next(e for e in wb.loop_health_snapshot()['recent'] if e['kind']=='reasoning_buffer_discarded')
            self.assertGreater(ev['buffered_items'], 512)

    def test_observation_restarts_only_once_without_changing_output(self):
        data = tiny_frames(NORMAL*3) + frames(WIRE, 17, MIXED*6) + turn(GOOD_CALL, 'tool_calls')
        with mock.patch.object(wb.time, 'monotonic', return_value=0):
            result = self.raw(http_stream(data))
        self.assertEqual(result, list(http_stream(data)))
        self.assertEqual(self.counts().get('reasoning_buffer_observation'), 1)
        self.assertEqual(self.counts().get('reasoning_stall_observed'), 1)
        self.assertNotIn('reasoning_loop_detected', self.counts())

    def test_observation_does_not_override_content_tool_or_disable_boundaries(self):
        for boundary in ({'content':'visible'}, GOOD_CALL, {'tool_calls':[{'index':0,'function':{'arguments':'{'}}]}):
            self.clear_events()
            data = [chunk(boundary)] + frames(WIRE, 17, MIXED*2) + turn()
            self.assertEqual(self.raw(data), data)
            self.assertNotIn('reasoning_buffer_observation', self.counts())
        for name in ('WB_OPS_STALL_OBSERVE','WB_OPS_REASONING_BUFFER'):
            self.clear_events();data = tiny_frames(NORMAL*3)+frames(WIRE, 17, MIXED*2)+turn()
            with mock.patch.dict(os.environ,{name:'0'}):self.assertEqual(self.raw(data),data)
            self.assertNotIn('reasoning_buffer_observation',self.counts())

    def test_observation_all_apis_success_and_double_loop_retry_budget(self):
        for api in MODES:
            data = tiny_frames(NORMAL*3)+frames(WIRE, 17, MIXED)+turn(GOOD_CALL,'tool_calls')
            with mock.patch.object(wb.time,'monotonic',return_value=0),mock.patch.object(wb,'open_upstream') as opened:
                h=self.invoke(api,http_stream(data))
            opened.assert_not_called();self.assertEqual(self.thought(h),NORMAL*3+MIXED)
            data=tiny_frames(NORMAL*3)+frames(WIRE,17,MIXED)+frames(WIRE,17,LOOP)
            with mock.patch.object(wb.time,'monotonic',return_value=0),mock.patch.object(wb,'open_upstream',return_value=(http_stream(data),Account('b'))) as opened:
                h=self.invoke(api,http_stream(data))
            self.assertEqual(opened.call_count,1);self.assertIn('reasoning_loop_detected',self.output(h))

    def test_observation_buffer_respects_time_and_transport_failure(self):
        clock=[0]
        prefix=list(http_stream(tiny_frames(NORMAL*3)+frames(WIRE,17,MIXED)))
        def source():
            yield from prefix
            clock[0]=3
            yield b': heartbeat\n'
            raise OSError('disconnect')
        g=wb._GuardedStream(source(),{'model':'deepseek-v4.1-flash','generation_deadline':None});out=[]
        try:
            with mock.patch.object(wb.time,'monotonic',side_effect=lambda:clock[0]),self.assertRaises(OSError):
                for raw in g:out.append(raw)
        finally:g.close()
        self.assertEqual(out,prefix+[b': heartbeat\n'])
        self.assertEqual(self.counts().get('reasoning_buffer_observation'),1)
        self.assertEqual(self.counts().get('reasoning_buffer_release_time'),1)

    def test_content_diagnostics_are_once_private_and_do_not_reclassify(self):
        for text,expected in (('<thinking>PRIVATE_ANALYSIS</thinking>Public answer','closed'),('<thinking>PRIVATE_ANALYSIS','unclosed')):
            for size in (1,17,128):
                self.clear_events()
                data=[chunk({'content':text[i:i+size]}) for i in range(0,len(text),size)]+turn()
                self.assertEqual(self.raw(data),data)
                rows=[e for e in wb.loop_health_snapshot()['recent'] if e['kind']=='thinking_content_summary']
                self.assertEqual(len(rows),1);self.assertEqual(rows[0]['closed_boundary'],int(expected=='closed'))
                self.assertEqual(rows[0]['content_chars'],len(text))
                self.assertFalse(rows[0]['loop_hit'])
                self.assertNotIn('PRIVATE_ANALYSIS',json.dumps(wb.loop_health_snapshot()))

    def test_content_loop_still_fails_without_retry_and_reports_summary(self):
        for api in MODES:
            self.clear_events();text='<thinking>'+LOOP
            data=[chunk({'content':text[i:i+17]}) for i in range(0,len(text),17)]+turn()
            with mock.patch.object(wb,'open_upstream') as opened:h=self.invoke(api,Stream(data))
            opened.assert_not_called();self.assertIn('content_loop_detected',self.output(h))
            rows=[e for e in wb.loop_health_snapshot()['recent'] if e['kind']=='thinking_content_summary']
            self.assertEqual(len(rows),1);self.assertTrue(rows[0]['loop_hit'])


if __name__=='__main__':
    suite=unittest.TestSuite(RecordTests(n) for n in RecordTests.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

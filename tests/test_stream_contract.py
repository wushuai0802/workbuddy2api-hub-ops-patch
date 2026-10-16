"""Iteration must own prefetched bytes; real ledgers survive fragmented errors."""
import copy
import json
import os
import time
import unittest
from unittest import mock

from test_integration import Account, BODY, Handler, MODEL, chunk, responses, wb
from test_guarded_sse_batching import ChunkSource
from test_v1710 import PAID, USAGE


class ProbedReadContractTests(unittest.TestCase):
    def test_raw_read_interfaces_cannot_skip_prefix_or_unread_remainder(self):
        methods = ('read', 'read1', 'readall', 'readinto', 'readinto1', 'readline', 'readlines', 'peek')
        first = chunk({'content': 'first'})
        tail = chunk({'content': 'next'}, finish='stop', usage=USAGE)
        for method in methods:
            for prefetched in (True, False):
                with self.subTest(method=method, prefetched=prefetched):
                    source = ChunkSource([first + tail] if prefetched else [first, tail])
                    # Even methods absent on this fake source would be dangerous
                    # if a future transport exposed them. None may be delegated.
                    forbidden = mock.Mock(side_effect=AssertionError('raw byte read bypass'))
                    if method != 'read1':
                        setattr(source, method, forbidden)
                    probed, _ = wb.probe_first_frame(source)
                    reads = source.reads
                    self.assertFalse(hasattr(probed, method))
                    with self.assertRaises(AttributeError):
                        getattr(probed, method)
                    self.assertEqual(source.reads, reads)
                    self.assertFalse(forbidden.called)
                    self.assertEqual(b''.join(wb.iter_sse_lines(probed)), first + tail)
                    probed.close()
                    self.assertTrue(source.closed)

    def test_metadata_and_lease_identity_remain_on_the_original_response(self):
        source = ChunkSource([chunk({'content': 'answer'}, finish='stop', usage=USAGE)])
        source._wb_billed_model = PAID
        source._ops_generation_deadline = time.monotonic() + 20
        source._pool_conn = object()
        source.fp = object()
        probed, _ = wb.probe_first_frame(source)
        try:
            self.assertEqual(probed._wb_billed_model, PAID)
            self.assertEqual(probed._ops_generation_deadline, source._ops_generation_deadline)
            self.assertIs(probed._pool_conn, source._pool_conn)
            self.assertIs(probed.fp, source.fp)
            self.assertIs(wb._stream_response(probed), source)
        finally:
            probed.close()

    def test_probe_keeps_usage_snapshot_and_flushes_before_next_raw_block(self):
        first = chunk({'content': 'first'}) + chunk(usage=USAGE)
        second = chunk({'content': 'last'}, finish='stop') + b'data: [DONE]\n\n'
        source = ChunkSource([first, second])
        probed, _ = wb.probe_first_frame(source)
        received = []
        flushed = []
        probed.set_usage_observer(lambda value: received.append(copy.deepcopy(value)))
        probed.set_before_read(lambda: flushed.append(source.reads))
        self.assertEqual(received[-1], USAGE)
        reads = source.reads
        for name in wb._ProbedStream._RAW_READ_METHODS:
            self.assertIsNone(getattr(probed, name, None))
        self.assertEqual(source.reads, reads)
        self.assertEqual(b''.join(wb.iter_sse_lines(probed, before_read=lambda: flushed.append(source.reads))), first + second)
        self.assertTrue(flushed)
        self.assertEqual(received[-1], USAGE)
        probed.close()


class FragmentedLedgerTests(unittest.TestCase):
    def test_error_terminal_retains_real_ledger_fields_in_eighteen_paths(self):
        frames = b''.join((chunk({'content': 'Visible partial output.'}),
                           chunk(finish='error', usage=USAGE), b'data: [DONE]\n\n'))
        for protocol in ('chat', 'responses', 'messages'):
            for streaming in (True, False):
                for size in (1, 17, 65536):
                    with self.subTest(protocol=protocol, streaming=streaming, block_bytes=size):
                        handler = Handler('/v1/' + protocol)
                        handler.key_entry = {'id': 'synthetic-key-id'}
                        source = ChunkSource([frames[n:n + size] for n in range(0, len(frames), size)])
                        source._wb_billed_model = PAID
                        args = [source, MODEL]
                        if protocol == 'responses':
                            args += [set(), {}]
                        args += [{'hash': 'synthetic'}, Account('synthetic-account'), time.time()]
                        with mock.patch.dict(os.environ, {'WB_OPS_OBSERVE': '0', 'WB_OPS_LOOP_RETRY': '0'}), \
                             mock.patch.object(wb, '_persist_usage') as persist, \
                             mock.patch.object(wb, 'open_upstream', side_effect=AssertionError('unexpected replay')) as opened, \
                             mock.patch.object(wb, 'apply_daily_token_limit'), mock.patch.object(wb, 'log'):
                            getattr(handler, '_' + protocol + ('_stream_response' if streaming else '_nonstream_response'))(
                                *args, base_body=copy.deepcopy(BODY), effort='high')
                        self.assertEqual(persist.call_count, 1)
                        row = persist.call_args.args[0]
                        self.assertEqual(row['model'], PAID)
                        self.assertEqual(row['requested_model'], MODEL)
                        self.assertEqual(row['total_tokens'], 31)
                        self.assertEqual(row['account'], 'synthetic-account')
                        self.assertEqual(row['key'], 'synthetic-key-id')
                        self.assertEqual(row['reasoning_effort'], 'high')
                        self.assertEqual(row['status'], 502)
                        self.assertNotEqual(row.get('outcome', 'failed'), 'completed')
                        self.assertTrue(source.closed)
                        opened.assert_not_called()
                        output = json.dumps(responses(handler) if streaming else handler.json_body)
                        self.assertIn('upstream_finish_error', output)


if __name__ == '__main__':
    unittest.main()

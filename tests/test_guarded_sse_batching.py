"""Probing/guards must retain byte batching, prompt flush and consumed usage."""
import json
import unittest
from unittest import mock

from test_integration import Account, BODY, Handler, MODEL, chunk, responses, turn, wb


class ChunkSource:
    def __init__(self, chunks, before_read=None):
        self.chunks = list(chunks)
        self.reads = 0
        self.before_read = before_read
        self.closed = False

    def read1(self, size):
        self.reads += 1
        if self.before_read:
            self.before_read(self.reads)
        if not self.chunks:
            return b''
        data = self.chunks.pop(0)
        if len(data) > size:
            self.chunks.insert(0, data[size:])
            data = data[:size]
        return data

    def __iter__(self):
        raise AssertionError('line-by-line source reading bypassed read1')

    def close(self):
        self.closed = True


class GuardedBatchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(wb.os.environ, {'WB_OPS_LOOP_RETRY': '0'}))
        self.enterContext(mock.patch.object(wb, 'record_usage'))
        self.enterContext(mock.patch.object(wb, 'record_error'))

    def test_probe_replays_exact_prefix_and_all_prefetched_remainder(self):
        wire = b''.join(turn({'content': 'First complete result.'}))
        source = ChunkSource([wire])
        probed, _ = wb.probe_first_frame(source)
        self.assertEqual(b''.join(wb.iter_sse_lines(probed)), wire)
        self.assertEqual(source.reads, 2)
        probed.close()

    def test_guarded_chat_batches_and_flushes_before_next_block_read(self):
        handler = Handler('/v1/chat/completions')
        first = b''.join(chunk({'content': 'segment-%d ' % n}) for n in range(24))
        second = b''.join(turn({'content': 'last-result'}))
        def check(reads):
            if reads == 2:
                self.assertIn(b'segment-0', b''.join(handler.written), 'first block held until next read')
        source = ChunkSource([first, second], check)
        handler._chat_stream_response(source, 'fixture-model', {}, Account(), wb.time.time())
        self.assertTrue(source.closed)
        self.assertEqual(source.reads, 2)
        choices = [choice for frame in responses(handler) for choice in frame.get('choices', [])]
        self.assertEqual(''.join(choice['delta'].get('content', '') for choice in choices),
                         ''.join('segment-%d ' % n for n in range(24)) + 'last-result')
        self.assertEqual([choice['finish_reason'] for choice in choices if choice.get('finish_reason')], ['stop'])
        self.assertLessEqual(len(handler.written), 3, 'one downstream write per event')
        self.assertEqual(b''.join(handler.written).count(b'[DONE]'), 1)

    def test_client_disconnect_stops_before_consuming_another_block(self):
        handler = Handler('/v1/chat/completions', writer_error=BrokenPipeError())
        source = ChunkSource([chunk({'content': 'First.'}), b''.join(turn({'content': 'Never consumed.'}))])
        with mock.patch.object(wb, 'open_upstream') as reopened:
            handler._chat_stream_response(source, 'fixture-model', {}, Account(), wb.time.time(), base_body=BODY)
        self.assertEqual(source.reads, 1)
        self.assertTrue(source.closed)
        reopened.assert_not_called()

    def test_usage_already_received_in_loop_block_is_preserved(self):
        loop = 'Let me write the actual tool call.\n' * 80
        usage = {'prompt_tokens': 71, 'completion_tokens': 22, 'total_tokens': 93}
        wire = chunk({'reasoning_content': loop}) + chunk(finish='stop', usage=usage) + b'data: [DONE]\n\n'
        for probe in (False, True):
            with self.subTest(probe=probe):
                source = ChunkSource([wire])
                upstream = wb.probe_first_frame(source)[0] if probe else source
                guard = wb._GuardedStream(upstream, {'model': MODEL, 'base_body': BODY})
                try:
                    frames = [json.loads(line[6:]) for block in guard for line in block.decode().splitlines()
                              if line.startswith('data: {')]
                finally:
                    guard.close()
                self.assertTrue(any(frame.get('error', {}).get('code') == 'reasoning_loop_detected' for frame in frames))
                self.assertIn(usage, [frame['usage'] for frame in frames if 'usage' in frame])
                self.assertEqual(source.reads, 1, 'guard drained additional upstream data after failure')

    def test_writer_bounds_queue_and_preserves_wire_bytes(self):
        handler = Handler()
        writer = wb._BufferedStreamWriter(handler)
        writer.LIMIT = 64
        parts = [b'a' * 30, b'b' * 20, b'c' * 100, b'd' * 17]
        for part in parts:
            writer.write(part)
            self.assertLessEqual(writer.size, 64)
        writer.flush()
        self.assertEqual(b''.join(handler.written), b''.join(parts))


if __name__ == '__main__':
    unittest.main()

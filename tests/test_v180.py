"""A saturated stderr channel cannot block guarded terminals, usage or slot release."""
from contextlib import ExitStack
import threading
import time
import unittest
from unittest import mock

from test_guarded_sse_batching import ChunkSource
from test_integration import Account, BODY, Handler, IntegrationTests, MODEL, chunk, wb


class BlockingStderr:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
    def write(self, text):
        self.entered.set()
        if not self.release.wait(4):
            raise AssertionError('test did not release synthetic stderr')
        return len(text)
    def flush(self): pass


class V180Tests(IntegrationTests):
    def test_blocked_stderr_keeps_three_stream_guards_and_usage_operational(self):
        blocked = BlockingStderr()
        dropped = wb._LOG_DROPPED
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(wb.sys, 'stderr', blocked))
            stack.enter_context(mock.patch.dict(wb._ACCOUNT_INFLIGHT, {}, clear=True))
            opened = stack.enter_context(mock.patch.object(wb, 'open_upstream', side_effect=AssertionError('unexpected retry')))
            try:
                wb.log('synthetic blocked stderr')
                self.assertTrue(blocked.entered.wait(1))
                for index in range(wb._LOG_QUEUE_MAX + 3):
                    wb.log('synthetic saturation %d' % index)
                self.assertGreater(wb._LOG_DROPPED, dropped)
                for api in ('chat', 'responses', 'messages'):
                    with self.subTest(api=api):
                        self.records.clear()
                        usage = {'prompt_tokens': 71, 'completion_tokens': 22, 'total_tokens': 93}
                        loop = 'Let me write the actual tool call.\n' * 80
                        source = ChunkSource([chunk({'reasoning_content': loop}) + chunk(finish='stop', usage=usage) + b'data: [DONE]\n\n'])
                        account = Account('bounded-log-fixture')
                        self.assertTrue(wb.account_slot_acquire(account, limit=1))
                        wb.bind_account_slot_to_response(source, account)
                        handler = Handler('/v1/' + api)
                        args = [source, MODEL]
                        if api == 'responses': args += [set(), {}]
                        args += [{'hash': 'synthetic'}, account, time.time()]
                        started = time.monotonic()
                        getattr(handler, '_' + api + '_stream_response')(*args, base_body=BODY)
                        self.assertLess(time.monotonic() - started, 1)
                        self.assertIn(b'reasoning_loop_detected', b''.join(handler.written))
                        self.assertTrue(source.closed)
                        self.assertEqual(wb.account_inflight_snapshot(), {})
                        self.assertEqual(len(self.records), 1)
                        kind, _, options = self.records[0]
                        self.assertEqual(kind, 'error')
                        self.assertEqual(options['usage']['total_tokens'], 93)
                opened.assert_not_called()
            finally:
                blocked.release.set()
                wb.flush_log_channel(2)


if __name__ == '__main__':
    suite = unittest.TestSuite(V180Tests(name) for name in V180Tests.__dict__ if name.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

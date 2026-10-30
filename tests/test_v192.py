"""Retained guarded terminals coexist with native opt-in transport switches."""
import json
import time
import unittest
import urllib.error
import io
from unittest import mock

from test_account_slot_lifecycle import AccountSlotTests, APIS
from test_guarded_sse_batching import ChunkSource
from test_integration import Account, MODEL, turn, wb


class V192Tests(AccountSlotTests):
    def test_done_stops_owned_streams_preserving_usage_and_slots(self):
        # The native switch remains off by default. Our older guarded streams
        # stop regardless; enabling it additionally covers native aggregation.
        for enabled in ('0', '1'):
            for api in APIS:
                if enabled == '0' and api.endswith('_json'):
                    continue
                self.records.clear()
                def before(reads):
                    if reads > 1:
                        raise AssertionError('read after terminal')
                source = ChunkSource([b''.join(turn({'content': 'complete answer'}, tokens=107))], before)
                account = Account('terminal-owner')
                with mock.patch.dict(wb.os.environ, {'WB_STOP_AT_SSE_DONE': enabled}), \
                        mock.patch.dict(wb._ACCOUNT_INFLIGHT, {}, clear=True):
                    self.assertTrue(wb.account_slot_acquire(account, limit=1))
                    wb.bind_account_slot_to_response(source, account)
                    output = self.invoke(api, source, account)
                    self.assertIn('complete answer', output, (enabled, api))
                    self.assertEqual(source.reads, 1, (enabled, api))
                    self.assertTrue(source.closed)
                    self.assertEqual(wb.account_inflight_snapshot(), {})
                    self.assertEqual(len(self.records), 1)
                    self.assertEqual(self.records[0][0], 'usage')
                    self.assertEqual(self.records[0][1][1]['total_tokens'], 107)

    def test_done_text_inside_json_does_not_end_a_response(self):
        for api in ('chat', 'responses', 'messages'):
            source = ChunkSource([b''.join(turn({'content': 'before [DONE] after'}, tokens=107))])
            with mock.patch.dict(wb.os.environ, {'WB_STOP_AT_SSE_DONE': '1'}):
                output = self.invoke(api, source, Account())
            self.assertIn('before [DONE] after', output)
            self.assertTrue(source.closed)

    def test_bounded_401_cache_is_retained_with_generic_cache_disabled(self):
        for enabled in ('0', '1'):
            body = b'{"code":401,"message":"synthetic token expired"}'
            error = urllib.error.HTTPError('https://example.invalid', 401, 'synthetic',
                {'WWW-Authenticate': 'Bearer error="invalid_token"'}, io.BytesIO(body))
            with mock.patch.dict(wb.os.environ, {'WB_CACHE_HTTP_ERROR_BODY': enabled}):
                label = wb._read_auth_error_message(error, time.monotonic() + 1, 1)
                self.assertTrue(wb.wb_accounts._auth_failure_text(label))
                self.assertEqual(wb.read_http_error(error, 6), body[:6])
                self.assertEqual(wb.read_http_error(error, 4096), body)


if __name__ == '__main__':
    suite = unittest.TestSuite(V192Tests(name) for name in V192Tests.__dict__ if name.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

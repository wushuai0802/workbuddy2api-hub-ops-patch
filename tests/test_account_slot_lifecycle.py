"""A response owns only its acquired account slot, including cancellation paths."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
from unittest import mock

from test_integration import BODY, Handler, IntegrationTests, MODEL, turn, wb
from test_guarded_sse_batching import ChunkSource
from test_loop_recovery import LOOP

APIS = ('chat', 'chat_json', 'responses', 'responses_json', 'messages', 'messages_json')


class AccountSlotTests(IntegrationTests):
    def context(self, stack, raw=None, limit=1, static=False):
        account = wb.wb_accounts.Account({'uid': 'slot-fixture', 'realm': 'intl',
            'accessToken': 'ck_synthetic_key' if static else 'synthetic-access',
            'refreshToken': '' if static else 'synthetic-refresh'})
        for name in ('note_success', 'note_error', 'note_failure', 'note_unknown_failure'):
            stack.enter_context(mock.patch.object(account, name, return_value=0))
        account.headers = mock.Mock(wraps=account.headers)
        raw = raw or ChunkSource([b''.join(turn({'content': 'answer'}, tokens=26))])
        pool = mock.Mock()
        pool.accounts = [account]
        pool.count_ready.return_value = 1
        pool.get.side_effect = lambda uid: account if uid == account.uid else None
        pool.pick_for_session.side_effect = lambda **kw: (
            account if account.uid not in kw['exclude'] and
            (kw.get('allowed') is None or account.uid in kw['allowed']) else None)
        stack.enter_context(mock.patch.object(wb, 'POOL', pool))
        stack.enter_context(mock.patch.dict(wb._ACCOUNT_INFLIGHT, {}, clear=True))
        for name, value in (('apply_daily_guards', None), ('restriction_hold_remaining', 0),
                            ('pinned_account', None), ('_free_model_credit_fallback_enabled', False)):
            stack.enter_context(mock.patch.object(wb, name, return_value=value))
        cap = stack.enter_context(mock.patch.object(wb.wb_settings, 'account_max_concurrent', return_value=limit))
        stack.enter_context(mock.patch.object(wb.wb_settings, 'egress_backoff_enabled', return_value=False))
        opened = stack.enter_context(mock.patch.object(wb.wb_accounts, 'urlopen', return_value=raw))
        return account, opened, cap, raw

    def invoke(self, api, source, account, disconnect=False):
        handler = Handler('/v1/' + api.split('_')[0], writer_error=BrokenPipeError() if disconnect else None)
        args = [source, MODEL]
        if api.startswith('responses'):
            args += [set(), {}]
        args += [{'hash': 'synthetic'}, account, time.time()]
        protocol = api.split('_')[0]
        method = '_' + protocol + ('_nonstream_response' if api.endswith('_json') else '_stream_response')
        try:
            getattr(handler, method)(*args, base_body=BODY, effort='high')
        except BrokenPipeError:
            if not disconnect:
                raise
        return json.dumps(handler.json_body) if handler.json_body is not None else b''.join(handler.written).decode()

    def test_slot_held_until_body_close_and_repeated_close_keeps_next_request(self):
        with ExitStack() as stack:
            account, _, _, raw = self.context(stack)
            wb.open_upstream(BODY, allowed_accounts={account.uid})
            self.assertEqual(wb.account_inflight_snapshot(), {account.uid: 1})
            raw.close()
            self.assertTrue(wb.account_slot_acquire(account, limit=1))
            raw.close()
            self.assertEqual(wb.account_inflight_snapshot(), {account.uid: 1})

    def test_turning_limit_off_does_not_release_older_counted_request(self):
        with ExitStack() as stack:
            account, opened, cap, first = self.context(stack)
            second = ChunkSource([b''.join(turn({'content': 'second'}))])
            opened.side_effect = [first, second]
            wb.open_upstream(BODY)
            cap.return_value = 0
            wb.open_upstream(BODY)
            second.close()
            self.assertEqual(wb.account_inflight_snapshot(), {account.uid: 1})
            first.close()
            self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_unlimited_admission_never_invokes_slot_release(self):
        with ExitStack() as stack:
            _, _, _, raw = self.context(stack, limit=0)
            with mock.patch.object(wb, 'account_slot_release') as release:
                wb.open_upstream(BODY)
                raw.close()
                release.assert_not_called()

    def test_post_open_bookkeeping_error_closes_unreturned_response(self):
        for failure in ('note_success', 'upstream_effort_of'):
            with self.subTest(failure=failure), ExitStack() as stack:
                account, _, _, raw = self.context(stack)
                owner = account if failure == 'note_success' else wb
                stack.enter_context(mock.patch.object(owner, failure, side_effect=ValueError('synthetic bookkeeping')))
                with self.assertRaises(Exception):
                    wb.open_upstream(BODY, pin_account=account)
                self.assertTrue(raw.closed)
                self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_request_preparation_error_releases_before_any_connection(self):
        with ExitStack() as stack:
            account, opened, _, _ = self.context(stack)
            account.headers.side_effect = ValueError('synthetic preparation')
            with self.assertRaisesRegex(ValueError, 'synthetic preparation'):
                wb.open_upstream(BODY)
            opened.assert_not_called()
            self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_pinned_busy_wait_raises_original_deadline_without_releasing_owner(self):
        with ExitStack() as stack:
            account, opened, _, _ = self.context(stack)
            wb.account_slot_acquire(account, limit=1)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                wb.open_upstream(BODY, pin_account=account, deadline_at=started + .06)
            self.assertLess(time.monotonic() - started, .5)
            opened.assert_not_called()
            self.assertEqual(wb.account_inflight_snapshot(), {account.uid: 1})

    def test_full_pool_is_busy_without_account_penalty(self):
        with ExitStack() as stack:
            account, opened, _, _ = self.context(stack)
            wb.account_slot_acquire(account, limit=1)
            with self.assertRaises(wb.RateLimited):
                wb.open_upstream(BODY)
            opened.assert_not_called()
            account.note_failure.assert_not_called()
            account.note_error.assert_not_called()
            self.assertEqual(wb.account_inflight_snapshot(), {account.uid: 1})

    def test_oauth_refresh_runs_without_slot_then_reacquires_same_allowed_account(self):
        with ExitStack() as stack:
            account, opened, _, raw = self.context(stack)
            opened.side_effect = [urllib.error.HTTPError('https://example.invalid', 401, 'expired', {}, io.BytesIO(b'{}')), raw]
            deadline = time.monotonic() + 2
            refresh_slots = []
            def refresh(*args, **kwargs):
                refresh_slots.append(wb.account_inflight_snapshot())
                self.assertLessEqual(kwargs['deadline_at'], deadline)
                return True
            with mock.patch.object(account, 'refresh_after_401', side_effect=refresh) as renewed:
                _, selected, _ = wb.open_upstream(BODY, deadline_at=deadline, allowed_accounts={account.uid})
                self.assertIs(selected, account)
                renewed.assert_called_once()
            self.assertEqual(refresh_slots, [{}])
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(wb.account_inflight_snapshot(), {account.uid: 1})
            raw.close()
            self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_base_exception_cancellation_releases_slot(self):
        with ExitStack() as stack:
            _, opened, _, _ = self.context(stack)
            opened.side_effect = KeyboardInterrupt('synthetic cancellation')
            with self.assertRaises(KeyboardInterrupt):
                wb.open_upstream(BODY)
            self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_static_key_401_never_refreshes_or_leaks(self):
        with ExitStack() as stack:
            account, opened, _, _ = self.context(stack, static=True)
            opened.side_effect = urllib.error.HTTPError('https://example.invalid', 401, 'bad key', {}, io.BytesIO(b'{}'))
            with mock.patch.object(account, 'refresh_after_401') as renew:
                with self.assertRaises(urllib.error.HTTPError):
                    wb.open_upstream(BODY, target_realm=account.realm)
                renew.assert_not_called()
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_concurrent_close_releases_only_this_response_slot(self):
        with ExitStack() as stack:
            account, _, _, raw = self.context(stack, limit=2)
            wb.open_upstream(BODY)
            wb.account_slot_acquire(account, limit=2)
            gate = threading.Barrier(8)
            def close():
                gate.wait(timeout=2)
                raw.close()
            threads = [threading.Thread(target=close) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
            self.assertEqual(wb.account_inflight_snapshot(), {account.uid: 1})

    def test_six_adapters_release_on_success_failure_and_disconnect(self):
        for api in APIS:
            for outcome in ('stop', 'error', 'disconnect'):
                with self.subTest(api=api, outcome=outcome), ExitStack() as stack:
                    raw = ChunkSource([b''.join(turn({'content': 'answer'}, 'error' if outcome == 'error' else 'stop'))])
                    account, _, _, _ = self.context(stack, raw)
                    wb.open_upstream(BODY)
                    self.invoke(api, raw, account, disconnect=outcome == 'disconnect')
                    self.assertTrue(raw.closed)
                    self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_six_adapters_close_before_safe_loop_recovery_admission(self):
        with mock.patch.dict(os.environ, {'WB_OPS_LOOP_RETRY': '1'}):
            for api in APIS:
                with self.subTest(api=api), ExitStack() as stack:
                    first = ChunkSource([b''.join(turn({'reasoning_content': LOOP}))])
                    second = ChunkSource([b''.join(turn({'content': 'recovered'}))])
                    account, opened, _, _ = self.context(stack, first)
                    def connect(*args, **kwargs):
                        self.assertEqual(wb.account_inflight_snapshot(), {account.uid: 1})
                        if opened.call_count == 1:
                            return first
                        self.assertTrue(first.closed)
                        return second
                    opened.side_effect = connect
                    wb.open_upstream(BODY)
                    self.assertIn('recovered', self.invoke(api, first, account))
                    self.assertEqual(opened.call_count, 2)
                    self.assertTrue(first.closed and second.closed)
                    self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_real_loopback_stream_holds_slot_and_keeps_single_gateway_id(self):
        source = Path(os.environ['WB_TEST_SOURCE']) / 'tests/_test_stream_single_id.py'
        code = source.read_text(encoding='utf8')
        needle = 'P._bootstrap_runtime(Args())'
        self.assertEqual(code.count(needle), 1)
        code = code.replace(needle, needle + '\nP.wb_settings.set_account_max_concurrent(ACCOUNTS, 1)')
        needle = '            payload = sse(CHUNKS)'
        self.assertEqual(code.count(needle), 1)
        code = code.replace(needle, '            assert P.account_inflight_snapshot().get("acct-stream-single-id") == 1\n' + needle)
        needle = '    gateway.shutdown()'
        self.assertEqual(code.count(needle), 1)
        code = code.replace(needle, '''    deadline = time.monotonic() + 1
    while P.account_inflight_snapshot() and time.monotonic() < deadline:
        time.sleep(.01)
    check("slot released after HTTP response", P.account_inflight_snapshot() == {})
''' + needle)
        result = subprocess.run([sys.executable, '-B', '-c', '__file__ = ' + repr(str(source)) + '\n' + code],
            cwd=source.parent.parent, capture_output=True, text=True, encoding='utf8', timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout[-4500:] + result.stderr[-2000:])


if __name__ == '__main__':
    suite = unittest.TestSuite(AccountSlotTests(name) for name in AccountSlotTests.__dict__ if name.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

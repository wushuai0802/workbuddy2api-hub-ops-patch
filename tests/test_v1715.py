"""Native identity/pinned routes must preserve bounded refresh and safe recovery."""
import io
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

from test_integration import BODY, MODEL, Stream, turn, wb


class V1715Tests(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.pool = wb.wb_accounts.AccountPool(state.name)
        self.accounts = [wb.wb_accounts.Account({'uid': uid, 'realm': 'intl',
                         'accessToken': 'synthetic-old-' + uid, 'refreshToken': 'synthetic-refresh'})
                         for uid in ('a', 'b')]
        for account in self.accounts:
            account.expires_at = time.time() + 3600
        self.pool.accounts = self.accounts
        self.enterContext(mock.patch.multiple(wb, POOL=self.pool, ACCOUNTS_DIR=state.name,
                          CURRENT_REALM='intl'))
        self.enterContext(mock.patch.object(wb, 'apply_daily_guards'))
        self.enterContext(mock.patch.object(wb, 'remaining_priority_enabled', return_value=False))
        self.enterContext(mock.patch.object(wb, 'log'))
        self.enterContext(mock.patch.object(wb, '_free_model_credit_fallback_enabled', return_value=False))
        wb.reset_restriction_state()
        self.addCleanup(wb.reset_restriction_state)
        wb._PINNED.ref = None; wb._PINNED.account = None
        self.addCleanup(self.clear_pin)

    @staticmethod
    def clear_pin():
        wb._PINNED.ref = None; wb._PINNED.account = None

    def open(self, **options):
        return wb.open_upstream(BODY, target_realm='intl', prebuilt_body=BODY, **options)

    def unauthorized(self, request):
        return urllib.error.HTTPError(request.full_url, 401, 'synthetic', {}, io.BytesIO(b'{}'))

    def test_successful_refresh_retries_same_account_with_exact_new_headers_and_original_deadline(self):
        first, second = self.accounts
        calls, budgets = [], []
        deadline = time.monotonic() + 20
        def refresh(token, timeout, deadline_at):
            budgets.append((token, timeout, deadline_at))
            first.access_token = 'synthetic-new'
            return True
        def send(request, **_):
            calls.append(request)
            if len(calls) == 1: raise self.unauthorized(request)
            return Stream(turn({'content': 'ok'}))
        with mock.patch.object(self.pool, 'pick_for_session', side_effect=[first, second]) as pick, \
             mock.patch.object(first, 'refresh_after_401', side_effect=refresh), \
             mock.patch.object(first, 'refresh') as background, \
             mock.patch.object(wb.wb_accounts, 'urlopen', side_effect=send):
            response, selected, _ = self.open(deadline_at=deadline)
        self.assertIs(selected, first); response.close()
        self.assertEqual(pick.call_count, 1)
        self.assertEqual(len(budgets), 1)
        self.assertEqual(budgets[0][0], 'synthetic-old-a')
        self.assertEqual(budgets[0][2], deadline)
        self.assertLessEqual(budgets[0][1], 20)
        self.assertEqual(calls[1].get_header('Authorization'), 'Bearer synthetic-new')
        self.assertEqual(dict(calls[1].wb_exact_headers)['Authorization'], 'Bearer synthetic-new')
        background.assert_not_called()

    def test_repeated_401_does_not_add_background_or_second_refresh(self):
        first = self.accounts[0]
        with mock.patch.object(self.pool, 'pick_for_session', side_effect=[first, None]), \
             mock.patch.object(first, 'refresh_after_401', return_value=True) as refresh, \
             mock.patch.object(first, 'refresh') as background, \
             mock.patch.object(wb.wb_accounts, 'urlopen', side_effect=lambda req, **_: (_ for _ in ()).throw(self.unauthorized(req))) as send:
            with self.assertRaises(urllib.error.HTTPError): self.open()
        self.assertEqual(send.call_count, 2)
        refresh.assert_called_once(); background.assert_not_called()

    def test_refresh_does_not_bypass_a_new_disabled_model(self):
        first = self.accounts[0]
        def refresh(*_, **__):
            first.disabled_models = frozenset({MODEL})
            return True
        with mock.patch.object(self.pool, 'pick_for_session', side_effect=[first, None]) as pick, \
             mock.patch.object(first, 'refresh_after_401', side_effect=refresh), \
             mock.patch.object(wb.wb_accounts, 'urlopen', side_effect=lambda req, **_: (_ for _ in ()).throw(self.unauthorized(req))) as send:
            with self.assertRaises(urllib.error.HTTPError): self.open()
        self.assertEqual(send.call_count, 1); self.assertEqual(pick.call_count, 2)
        self.assertIn('a', pick.call_args.kwargs['exclude'])

    def test_refresh_html_401_does_not_expire_a_healthy_account(self):
        account = self.accounts[0]
        error = urllib.error.HTTPError('https://synthetic.invalid', 401, 'synthetic', {},
                                      io.BytesIO(b'<html>proxy authorization 401</html>'))
        # The bounded transport captures this body before closing its watchdog.
        error._ops_auth_error_body = b'<html>proxy authorization 401</html>'
        with mock.patch.object(wb.wb_accounts, 'http_json', side_effect=error):
            self.assertFalse(account.refresh_after_401(account.access_token))
        self.assertFalse(account.token_expired)
        self.assertNotIn('登录已过期', account.last_error)
        self.assertTrue(error.fp.closed)

    def test_refresh_json_401_marks_an_expired_credential(self):
        account = self.accounts[0]
        error = urllib.error.HTTPError('https://synthetic.invalid', 401, 'synthetic', {},
                                      io.BytesIO(b'{"msg":"expired token"}'))
        error._ops_auth_error_body = b'{"msg":"expired token"}'
        with mock.patch.object(wb.wb_accounts, 'http_json', side_effect=error):
            self.assertFalse(account.refresh_after_401(account.access_token))
        self.assertTrue(account.token_expired)
        self.assertTrue(error.fp.closed)

    def test_pinned_account_can_refresh_once_without_rotating(self):
        first = self.accounts[0]
        wb._PINNED.account = first; wb._PINNED.ref = 'a'
        def send(request, **_):
            if request.get_header('Authorization') != 'Bearer synthetic-new': raise self.unauthorized(request)
            return Stream(turn({'content': 'ok'}))
        def refresh(*_, **__):
            first.access_token = 'synthetic-new'; return True
        with mock.patch.object(first, 'refresh_after_401', side_effect=refresh) as refresh_mock, \
             mock.patch.object(self.pool, 'pick_for_session') as pick, \
             mock.patch.object(wb.wb_accounts, 'urlopen', side_effect=send) as sender:
            response, selected, _ = self.open(); response.close()
        self.assertIs(selected, first)
        self.assertEqual(sender.call_count, 2); refresh_mock.assert_called_once(); pick.assert_not_called()

    def test_excluded_pinned_account_never_sends(self):
        wb._PINNED.account = self.accounts[0]
        with mock.patch.object(wb.wb_accounts, 'urlopen') as send:
            with self.assertRaises(wb.PinnedAccountError): self.open(exclude_uids={'a'})
        send.assert_not_called()

    def test_pin_survives_recovery_callback_on_a_different_worker(self):
        wb._PINNED.account = self.accounts[0]
        callback = wb._make_retry_call(BODY, None, 'intl')
        results = []
        def worker():
            self.assertIsNone(wb.pinned_account())
            try: callback({'a'}, deadline_at=time.monotonic() + 10)
            except wb.PinnedAccountError as error: results.append(error.account_uid)
        with mock.patch.object(wb, 'open_upstream') as send:
            thread = threading.Thread(target=worker); thread.start(); thread.join(3)
        self.assertFalse(thread.is_alive()); self.assertEqual(results, ['a']); send.assert_not_called()

    def test_pinned_6004_does_not_switch_to_a_paid_model(self):
        first = self.accounts[0]
        wb._PINNED.account = first
        error = urllib.error.HTTPError('https://synthetic.invalid', 429, 'synthetic', {},
                                      io.BytesIO(b'{"code":6004,"msg":"quota"}'))
        with mock.patch.object(wb, '_free_model_credit_fallback_enabled', return_value=True), \
             mock.patch.object(wb.wb_accounts, 'urlopen', side_effect=error) as send:
            with self.assertRaises(wb.RateLimited): self.open()
        self.assertEqual(send.call_count, 1)


if __name__ == '__main__': unittest.main()

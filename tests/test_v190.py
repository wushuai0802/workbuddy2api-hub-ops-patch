"""v1.9.0 authentication evidence, free-model cooldowns and failed-open billing.

Cases are independently reproduced against the merged baseline. The supplied
20261028-r1 bundle motivated these checks; all fixtures here are synthetic.
"""
from contextlib import ExitStack
import email.message
import io
import threading
import time
import unittest
import urllib.error
from unittest import mock

from test_account_slot_lifecycle import AccountSlotTests, APIS
from test_entry_observation import EntryObservationTests
from test_guarded_sse_batching import ChunkSource
from test_integration import Account, BODY, Handler, MODEL, turn, wb
from test_v1710 import PAID

A = wb.wb_accounts


def rejection(challenge=None, body=b'<html>401 invalid_token proxy page</html>'):
    headers = email.message.Message()
    if challenge:
        headers['WWW-Authenticate'] = challenge
    return urllib.error.HTTPError('https://example.invalid', 401, 'synthetic', headers, io.BytesIO(body))


class V190Tests(AccountSlotTests):
    def cold_free_account(self, uid='eligible'):
        account = A.Account({'uid': uid, 'realm': 'intl', 'accessToken': 'synthetic'})
        account.credits = {'remain': 2}
        account.reserve_credits = 10
        account.free_models = {MODEL}
        account.arm_model_cooldown(MODEL, time.time() + 100)
        return account

    def test_only_bearer_error_parameter_overrides_html(self):
        cases = [(None, False), ('Basic realm="invalid_token"', False),
                 ('Bearer realm="error=invalid_token"', False),
                 ('Basic error="invalid_token", Bearer realm="test"', False),
                 ('Basic realm="test", Bearer error = "invalid_token"', True),
                 ('Bearer realm="test", error="invalid_request"', False),
                 ('Bearer realm="test", error="invalid_token"', True)]
        for header, expected in cases:
            with self.subTest(header=header):
                error = rejection(header)
                self.addCleanup(error.close)
                label = A.http_error_message(error, error.read().decode())
                self.assertEqual(A._auth_failure_text(label), expected)

    def test_html_cannot_forge_challenge_prefix(self):
        error = rejection(body=b'<html>www-authenticate: Bearer error="invalid_token"): help</html>')
        self.addCleanup(error.close)
        self.assertFalse(A._auth_failure_text(A.http_error_message(error, error.read().decode())))

    def test_header_mapping_is_case_tolerant(self):
        error = rejection()
        self.addCleanup(error.close)
        error.headers = {'WWW-Authenticate': 'Bearer error="invalid_token"'}
        self.assertTrue(A._auth_failure_text(A.http_error_message(error, '<html>proxy</html>')))

    def test_static_model_401_preserves_challenge_without_oauth(self):
        for header, expected in ((None, False), ('Basic realm="invalid_token"', False),
                                 ('Bearer error="invalid_token"', True)):
            with ExitStack() as stack:
                account, opened, _, _ = self.context(stack, static=True)
                account.note_error.side_effect = lambda *args, **kwargs: A.Account.note_error(account, *args, **kwargs)
                opened.side_effect = rejection(header)
                plain = stack.enter_context(mock.patch.object(account, 'refresh'))
                refresh = stack.enter_context(mock.patch.object(account, 'refresh_after_401'))
                with self.assertRaises(urllib.error.HTTPError):
                    wb.open_upstream(BODY, target_realm=account.realm,
                                     allowed_accounts={account.uid}, deadline_at=time.monotonic() + 1)
                self.assertEqual(account.token_expired, expected)
                plain.assert_not_called()
                refresh.assert_not_called()
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_model_401_body_deadline_does_not_refresh_or_replay(self):
        class QuietBody(io.BytesIO):
            def __init__(self):
                super().__init__()
                self.closed_event = threading.Event()
            def read(self, size=-1):
                self.closed_event.wait(2)
                return b''
            def close(self):
                self.closed_event.set()
                super().close()
        with ExitStack() as stack:
            account, opened, _, _ = self.context(stack)
            opened.side_effect = urllib.error.HTTPError('https://example.invalid', 401, 'quiet', {}, QuietBody())
            refresh = stack.enter_context(mock.patch.object(account, 'refresh_after_401'))
            with self.assertRaises(TimeoutError):
                wb.open_upstream(BODY, deadline_at=time.monotonic() + .05)
            refresh.assert_not_called()
            self.assertEqual(opened.call_count, 1)
            account.note_error.assert_not_called()
            self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_refresh_challenge_survives_closed_html_or_overflow(self):
        for body in (b'<html>gateway</html>', b'x' * 4097):
            account = A.Account({'uid': 'refresh', 'realm': 'intl',
                                 'accessToken': 'synthetic', 'refreshToken': 'synthetic-refresh'})
            error = rejection('Bearer error="invalid_token"', body)
            error._ops_auth_error_body = None
            error.close()
            with mock.patch.object(A, 'http_json', side_effect=error):
                self.assertFalse(account._refresh_locked(deadline_at=time.monotonic() + 1))
            self.assertTrue(account.token_expired)

    def test_refresh_proxy_or_size_error_keeps_credential(self):
        for body in (b'<html>401 invalid_token</html>', b'x' * 4097):
            account = A.Account({'uid': 'refresh', 'realm': 'intl',
                                 'accessToken': 'synthetic', 'refreshToken': 'synthetic-refresh'})
            error = rejection(body=body)
            error._ops_auth_error_body = body if len(body) <= 4096 else None
            error.close()
            with mock.patch.object(A, 'http_json', side_effect=error):
                self.assertFalse(account._refresh_locked(deadline_at=time.monotonic() + 1))
            self.assertFalse(account.token_expired)

    def test_free_model_cooldown_uses_same_reserve_exemption_as_ready(self):
        account = self.cold_free_account()
        self.assertFalse(account.reserve_blocked(MODEL))
        self.assertTrue(account.blocked_by_model_cooldown(MODEL))
        account.token_expired = True
        self.assertFalse(account.blocked_by_model_cooldown(MODEL))
        account.token_expired = False
        account.enabled = False
        self.assertFalse(account.blocked_by_model_cooldown(MODEL))
        account.enabled = True
        account.free_models = set()
        self.assertFalse(account.blocked_by_model_cooldown(MODEL))

    def test_restricted_web_activity_is_a_failed_block_not_completed_skip(self):
        account = self.cold_free_account()
        account.restricted_until = time.time() + 100
        with mock.patch.object(account, '_daily_chat_web_upstream') as opened:
            result = account.daily_chat_web()
        self.assertFalse(result['ok'])
        self.assertTrue(result['blocked'])
        opened.assert_not_called()

    def test_cold_bound_free_account_returns_429_without_connection(self):
        with ExitStack() as stack:
            _, opened, _, _ = self.context(stack)
            account = self.cold_free_account()
            other = self.cold_free_account('excluded')
            other.token_expired = True
            wb.POOL.accounts = [account, other]
            wb.POOL.count_ready.return_value = 0
            wb.POOL.pick_for_session.side_effect = lambda **kwargs: None
            stack.enter_context(mock.patch.object(wb, 'realm_model_throttled', return_value=(False, 0)))
            with self.assertRaises(wb.RateLimited) as raised:
                wb.open_upstream(BODY, allowed_accounts={account.uid}, deadline_at=time.monotonic() + 1)
            self.assertGreater(raised.exception.wait, 0)
            opened.assert_not_called()
            self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_failed_paid_fallback_keeps_actual_model_and_one_error_row(self):
        with ExitStack() as stack:
            account, opened, _, _ = self.context(stack)
            stack.enter_context(mock.patch.object(wb, '_free_model_credit_fallback_enabled', return_value=True))
            account.cap_until = mock.Mock(return_value=time.time() + 100)
            wb.POOL.pick_for_session.side_effect = lambda **kw: account if kw.get('model') == PAID and not kw['exclude'] else None
            opened.side_effect = ValueError('synthetic failed connection')
            with self.assertRaises(ValueError) as raised:
                wb.open_upstream(BODY, allowed_accounts={account.uid}, deadline_at=time.monotonic() + 1)
            self.assertEqual(getattr(raised.exception, '_wb_billed_model', None), PAID)
            wb.upstream_failure_response(Handler(), MODEL, raised.exception, time.time(), 'fixture-key')
            self.assertEqual(len(self.records), 1)
            _, args, kwargs = self.records[0]
            self.assertEqual(args[0], PAID)
            self.assertEqual(kwargs['requested_model'], MODEL)
            self.assertEqual(kwargs['account'], account.uid)
            self.assertEqual(kwargs['key'], 'fixture-key')
            self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_all_entry_protocols_attribute_paid_open_errors(self):
        for protocol in ('chat', 'responses', 'messages'):
            self.records.clear()
            error = wb.RateLimited(detail='synthetic quota')
            error.account_uid = 'actual-account'
            error._wb_billed_model = PAID
            handler = EntryObservationTests.invoke(self, protocol, error)
            self.assertEqual(handler.status, 429)
            self.assertEqual(len(self.records), 1)
            _, args, kwargs = self.records[0]
            self.assertEqual(args[0], PAID)
            self.assertEqual(kwargs['requested_model'], MODEL)
            self.assertEqual(kwargs['account'], 'actual-account')

    def test_stream_failures_keep_received_usage_once_in_six_paths(self):
        for api in APIS:
            self.records.clear()
            source = ChunkSource([b''.join(turn({'content': 'partial'}, 'error', tokens=123))])
            source._wb_billed_model = PAID
            output = self.invoke(api, source, Account())
            self.assertIn('upstream_finish_error', output)
            self.assertEqual(len(self.records), 1)
            kind, args, kwargs = self.records[0]
            self.assertEqual(kind, 'error')
            self.assertEqual(args[0], PAID)
            self.assertEqual(kwargs['usage']['total_tokens'], 123)


if __name__ == '__main__':
    suite = unittest.TestSuite(V190Tests(name) for name in V190Tests.__dict__ if name.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

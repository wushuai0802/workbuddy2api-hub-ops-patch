"""Offline regressions for the NAS in-request token-refresh extension."""
import concurrent.futures
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wb_accounts as accounts_mod


class Account:
    def __init__(self, uid, refresh_ok=True, always_401=False):
        self.uid, self.realm, self.proxy = uid, "intl", ""
        self.product, self.enabled, self.admitted = "cli", True, True
        self.access_token, self.refresh_token = "old-" + uid, "refresh-" + uid
        self.refresh_ok, self.always_401 = refresh_ok, always_401
        self.refreshes, self.errors, self.budgets = 0, [], []
        self.on_refresh = lambda: None

    def chat_base_url(self):
        return "https://example.invalid"

    def headers(self, purpose="chat"):
        return {"Authorization": "Bearer " + self.access_token}

    def refresh_after_401(self, rejected_access_token, timeout=30):
        self.refreshes += 1
        self.budgets.append(timeout)
        self.on_refresh()
        if not self.refresh_ok:
            return False
        self.access_token = "new-" + self.uid
        return True

    def clear_error(self, model=None):
        pass

    def note_success(self, model=None):
        pass

    def note_error(self, *args, **kwargs):
        self.errors.append((args, kwargs))

    note_failure = note_error
    note_unknown_failure = note_error
    note_soft_rate = note_error
    note_balance_cooled = note_error


class Pool:
    def __init__(self, accounts):
        self.accounts = accounts
        self.affinity = type("Affinity", (), {"unbind": lambda self, key: None})()

    def count_ready(self, realm=None, model=None):
        return len(self.accounts)

    def pick_for_session(self, realm=None, session_key=None, exclude=None, model=None):
        return next((a for a in self.accounts
                     if a.uid not in (exclude or ()) and a.admitted and a.enabled), None)


class AuthRefreshTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.failures, self.counters = [], [], []
        self.tempdir = tempfile.TemporaryDirectory(prefix="wb-auth-refresh-")
        self.addCleanup(self.tempdir.cleanup)

    def run_request(self, accounts, deadline_at=None):
        # Keep account-only cases independent of the proxy module; importing it
        # here also lets its settings directory be isolated for every request.
        import wb_proxy as wb

        def open_fake(req, timeout=30, proxy=""):
            token = req.headers["Authorization"]
            self.calls.append(token)
            account = next(a for a in accounts if token.endswith("-" + a.uid))
            if token.startswith("Bearer old-") or account.always_401:
                error = urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"unauthorized"))
                self.failures.append(error)
                raise error
            return io.BytesIO(b"ok")

        with patch.multiple(wb, POOL=Pool(accounts), ACCOUNTS_DIR=self.tempdir.name,
                            apply_daily_token_limit=lambda: None,
                            apply_daily_credit_limit=lambda: None,
                            apply_model_daily_token_limit=lambda: None,
                            auto_switch_product_enabled=lambda: False,
                            prompt_cache_key_enabled=lambda: False,
                            retry_bump=self.counters.append,
                            log=lambda *a, **k: None), patch.object(accounts_mod, "urlopen", open_fake):
            return wb.open_upstream({"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "ok"}]},
                                    session_key="test-session", target_realm="intl", deadline_at=deadline_at)

    def test_refresh_retries_same_account_without_cooldown(self):
        a = Account("a")
        _, chosen, _effort = self.run_request([a])
        self.assertIs(chosen, a)
        self.assertEqual(self.calls, ["Bearer old-a", "Bearer new-a"])
        self.assertEqual(a.refreshes, 1)
        self.assertFalse(a.errors)
        self.assertTrue(all(error.fp.closed for error in self.failures))

    def test_failed_refresh_rotates(self):
        a, b = Account("a", refresh_ok=False), Account("b")
        _, chosen, _effort = self.run_request([a, b])
        self.assertIs(chosen, b)
        self.assertEqual(a.refreshes, 1)
        self.assertEqual(len(a.errors), 1)
        self.assertEqual(b.refreshes, 1)

    def test_repeated_401_is_bounded(self):
        a = Account("a", always_401=True)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.run_request([a])
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(a.refreshes, 1)
        self.assertEqual(len(self.calls), 2)

    def test_without_refresh_token_rotates(self):
        a, b = Account("a"), Account("b")
        a.refresh_token = ""
        _, chosen, _effort = self.run_request([a, b])
        self.assertIs(chosen, b)
        self.assertEqual(a.refreshes, 0)

    def test_retry_uses_official_admission(self):
        a, b = Account("a"), Account("b")
        a.on_refresh = lambda: setattr(a, "admitted", False)
        _, chosen, _effort = self.run_request([a, b])
        self.assertIs(chosen, b)
        self.assertNotIn("Bearer new-a", self.calls)

    def test_refresh_respects_request_deadline(self):
        a = Account("a")
        a.on_refresh = lambda: time.sleep(.03)
        with self.assertRaises(TimeoutError):
            self.run_request([a], deadline_at=time.monotonic() + .02)
        self.assertEqual(len(self.calls), 1)
        self.assertGreater(a.budgets[0], 0)
        self.assertLessEqual(a.budgets[0], .02)

    def real_account(self):
        return accounts_mod.Account({"uid": "test-real", "realm": "intl",
                                     "accessToken": "old-real", "refreshToken": "refresh-real"})

    def test_concurrent_refresh_reuses_new_token(self):
        account = self.real_account()
        gate = threading.Barrier(5)
        calls = []

        def refresh_fake(*args, **kwargs):
            calls.append(kwargs)
            time.sleep(.02)
            return {"data": {"accessToken": "new-real", "refreshToken": "rotated-real"}}

        def request():
            gate.wait(timeout=2)
            return account.refresh_after_401("old-real", timeout=1)

        with patch.object(accounts_mod, "http_json", refresh_fake):
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(lambda _: request(), range(5)))
        self.assertTrue(all(results))
        self.assertEqual(len(calls), 1)
        self.assertEqual(account.refresh_token, "rotated-real")
        self.assertLessEqual(calls[0]["timeout"], 1)

    def test_refresh_lock_wait_is_bounded(self):
        account = self.real_account()
        account._refresh_lock.acquire()
        try:
            start = time.monotonic()
            self.assertFalse(account.refresh_after_401("old-real", timeout=.02))
            self.assertLess(time.monotonic() - start, .3)
        finally:
            account._refresh_lock.release()

    def test_new_token_does_not_clear_model_cooldown(self):
        account = self.real_account()
        account.access_token = "new-real"
        account.model_cooldowns["deepseek-v4.1-flash"] = time.time() + 60
        with patch.object(accounts_mod, "http_json", side_effect=AssertionError("unexpected refresh")):
            self.assertTrue(account.refresh_after_401("old-real"))
        self.assertIn("deepseek-v4.1-flash", account.model_cooldowns)

    def test_refresh_preserves_new_governance_and_credit_guards(self):
        for already_refreshed in (False, True):
            with self.subTest(already_refreshed=already_refreshed):
                account = self.real_account()
                if already_refreshed:
                    account.access_token = "new-real"
                fields = {
                    "soft_streak": 2, "fails": 3, "degrade_count": 5,
                    "breaker_until": time.time() + 600,
                    "degrade_until": time.time() + 300,
                    "balance_until": time.time() + 900,
                    "credits": {"remain": 14, "updated_at": time.time() - 3600,
                                "packages": [{"remain": 14, "days_left": 2}]},
                    "reserve_credits": 10, "expiring_window_days": 7,
                    "daily_credit_limit": 20, "daily_credits_today": 20,
                    "daily_token_limit": 100, "daily_tokens_today": 100,
                    "model_daily_token_limit": 50,
                    "model_daily_tokens": {"deepseek-v4.1-flash": 50},
                    "enterprise_id": "test-enterprise",
                }
                for name, value in fields.items():
                    setattr(account, name, value)
                credits_lock = account._credits_lock
                with patch.object(accounts_mod, "http_json", return_value={
                        "data": {"accessToken": "new-real"}}) as refresh:
                    self.assertTrue(account.refresh_after_401("old-real", timeout=1))
                for name, value in fields.items():
                    self.assertEqual(getattr(account, name), value, name)
                self.assertIs(account._credits_lock, credits_lock)
                self.assertFalse(account.ready(model="deepseek-v4.1-flash"))
                self.assertEqual(refresh.call_count, 0 if already_refreshed else 1)
                if not already_refreshed:
                    self.assertEqual(refresh.call_args.kwargs["headers"]["X-Enterprise-Id"],
                                     "test-enterprise")

    def test_failed_refresh_releases_lock_and_preserves_token(self):
        account = self.real_account()
        with patch.object(accounts_mod, "http_json", side_effect=RuntimeError("offline failure")):
            self.assertFalse(account.refresh_after_401("old-real", timeout=1))
        self.assertEqual(account.access_token, "old-real")
        self.assertIn("offline failure", account.last_error)
        self.assertTrue(account._refresh_lock.acquire(blocking=False))
        account._refresh_lock.release()

    def test_expired_refresh_budget_never_calls_network(self):
        account = self.real_account()
        with patch.object(accounts_mod, "http_json", side_effect=AssertionError("unexpected refresh")):
            self.assertFalse(account.refresh_after_401("old-real", timeout=0))

    def test_refresh_receives_remaining_budget_after_lock_wait(self):
        account = self.real_account()

        class Lock:
            def acquire(self, timeout):
                self.timeout = timeout
                return True

            def release(self):
                self.released = True

        lock = account._refresh_lock = Lock()
        with patch.object(accounts_mod.time, "monotonic", side_effect=[100, 100.25]), \
                patch.object(accounts_mod, "http_json", return_value={
                    "data": {"accessToken": "new-real"}}) as refresh:
            self.assertTrue(account.refresh_after_401("old-real", timeout=1))
        self.assertEqual(lock.timeout, 1)
        self.assertTrue(lock.released)
        self.assertEqual(refresh.call_args.kwargs["timeout"], .75)

    def test_regular_refresh_keeps_default_timeout_and_proxy(self):
        account = self.real_account()
        account.proxy = "http://127.0.0.1:17901"
        with patch.object(accounts_mod, "http_json", return_value={
                "data": {"accessToken": "new-real"}}) as refresh:
            self.assertTrue(account.refresh())
        self.assertEqual(refresh.call_args.kwargs["timeout"], 30)
        self.assertEqual(refresh.call_args.kwargs["proxy"], account.proxy)


if __name__ == "__main__":
    unittest.main()

"""Connection preparation and late headers cannot spend an expired budget."""
from contextlib import ExitStack
import unittest
from unittest import mock

from test_account_slot_lifecycle import AccountSlotTests
from test_integration import BODY, wb


class ConnectionDeadlineTests(AccountSlotTests):
    def assert_no_account_penalty(self, account):
        account.note_error.assert_not_called()
        account.note_failure.assert_not_called()
        account.note_unknown_failure.assert_not_called()
        account.note_success.assert_not_called()
        self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_preparation_exhausts_deadline_without_opening_a_connection(self):
        with ExitStack() as stack:
            account, opened, _, raw = self.context(stack)
            clock = [100.]
            stack.enter_context(mock.patch.object(wb.time, 'monotonic', side_effect=lambda: clock[0]))
            def headers(**kwargs):
                clock[0] = 101.
                return {}
            account.headers.side_effect = headers
            with self.assertRaises(TimeoutError):
                wb.open_upstream(BODY, deadline_at=100.5)
            opened.assert_not_called()
            self.assertFalse(raw.closed, 'an unopened source was incorrectly consumed')
            self.assert_no_account_penalty(account)

    def test_late_headers_close_response_without_success_or_replay(self):
        with ExitStack() as stack:
            account, opened, _, raw = self.context(stack)
            clock = [100.]
            stack.enter_context(mock.patch.object(wb.time, 'monotonic', side_effect=lambda: clock[0]))
            def late(*args, **kwargs):
                self.assertGreater(kwargs['timeout'], 0)
                self.assertLessEqual(kwargs['timeout'], .5)
                clock[0] = 101.
                return raw
            opened.side_effect = late
            with self.assertRaises(TimeoutError):
                wb.open_upstream(BODY, deadline_at=100.5)
            self.assertEqual(opened.call_count, 1)
            self.assertTrue(raw.closed)
            self.assert_no_account_penalty(account)

    def test_expired_socket_error_stops_without_retry_or_account_penalty(self):
        with ExitStack() as stack:
            account, opened, _, _ = self.context(stack)
            clock = [100.]
            stack.enter_context(mock.patch.object(wb.time, 'monotonic', side_effect=lambda: clock[0]))
            sleep = stack.enter_context(mock.patch.object(wb.time, 'sleep'))
            def expired(*args, **kwargs):
                clock[0] = 101.
                raise TimeoutError('synthetic header timeout')
            opened.side_effect = expired
            with self.assertRaises(TimeoutError):
                wb.open_upstream(BODY, deadline_at=100.5)
            self.assertEqual(opened.call_count, 1)
            sleep.assert_not_called()
            self.assert_no_account_penalty(account)


if __name__ == '__main__':
    suite = unittest.TestSuite(ConnectionDeadlineTests(name) for name in ConnectionDeadlineTests.__dict__ if name.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

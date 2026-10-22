"""Reference-inspired fallback checks on the real six adapter entry paths."""
import copy
import json
import os
import time
import unittest
from contextlib import ExitStack
from unittest import mock

from test_integration import Account, BODY, MODEL, Handler, Stream, turn, wb
from test_v1617 import MigrationTests
from test_v1710 import PAID
from test_loop_recovery import LOOP
from test_guarded_sse_batching import ChunkSource

APIS = ('chat', 'chat_json', 'responses', 'responses_json', 'messages', 'messages_json')


class ManagedSource(ChunkSource):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class RealmBoundaryTests(MigrationTests):
    def enable(self, stack):
        pool = mock.Mock(); pool.count_ready.return_value = 2
        stack.enter_context(mock.patch.object(wb, 'POOL', pool))
        stack.enter_context(mock.patch.object(wb, 'realm_fallback_enabled', return_value=True))
        stack.enter_context(mock.patch.object(wb, 'restriction_hold_remaining', return_value=0))
        stack.enter_context(mock.patch.object(wb, 'pinned_account', return_value=None))
        return pool

    def dispatch(self, api):
        h = Handler('/v1/messages' if api.startswith('messages') else '/v1/chat/completions')
        h.key_entry = {'id': 'synthetic-key', 'accounts': ['cn-first', 'cn-second']}
        h._request_realm = lambda: None
        h._cross_realm_error = lambda *args: None
        h._banned_model_error = lambda *args: None
        h._key_model_error = lambda *args: None
        body = copy.deepcopy(BODY); body['stream'] = not api.endswith('_json')
        if api.startswith('responses'):
            body['input'] = 'test'; body.pop('messages')
            wb.Handler._handle_responses(h, body)
        elif api.startswith('messages'):
            body['max_tokens'] = 100
            wb.Handler._handle_messages(h, body)
        else:
            wb.Handler._dispatch_chat_post(h, '/v1/chat/completions', body)
        return h, json.dumps(h.json_body) if h.json_body is not None else b''.join(h.written).decode()

    def test_implicit_connection_budget_is_shared_and_uses_header_setting(self):
        with ExitStack() as stack:
            self.enable(stack)
            stack.enter_context(mock.patch.object(wb.time, 'monotonic', return_value=100.))
            stack.enter_context(mock.patch.object(wb, 'upstream_timeouts', return_value=(120., 300.)))
            opened = stack.enter_context(mock.patch.object(wb, 'open_upstream', side_effect=[wb.RateLimited(None, 'quota'), (None, None, None)]))
            wb.open_upstream_with_fallback(BODY, target_realm='intl')
            self.assertEqual([c.kwargs.get('deadline_at') for c in opened.call_args_list], [220., 220.])

    def test_disabled_fallback_keeps_the_existing_no_deadline_contract(self):
        with mock.patch.object(wb, 'realm_fallback_enabled', return_value=False), \
             mock.patch.object(wb, 'open_upstream', return_value=(None, None, None)) as opened:
            wb.open_upstream_with_fallback(BODY, target_realm='intl')
        self.assertNotIn('deadline_at', opened.call_args.kwargs)

    def test_domestic_restriction_hold_stops_before_selecting_backup(self):
        with ExitStack() as stack:
            pool = self.enable(stack)
            stack.enter_context(mock.patch.object(wb, 'restriction_hold_remaining', side_effect=lambda realm: 60 if realm == 'cn' else 0))
            opened = stack.enter_context(mock.patch.object(wb, 'open_upstream', side_effect=[wb.RateLimited(None, 'quota'), (None, None, None)]))
            with self.assertRaises(wb.RateLimited): wb.open_upstream_with_fallback(BODY, target_realm='intl')
            self.assertEqual(opened.call_count, 1)
            pool.count_ready.assert_not_called()

    def test_backup_eligibility_cannot_spend_the_remaining_deadline(self):
        clock = [100.]
        with ExitStack() as stack:
            pool = self.enable(stack)
            stack.enter_context(mock.patch.object(wb.time, 'monotonic', side_effect=lambda: clock[0]))
            def slow_ready(**kwargs): clock[0] = 106.; return 1
            pool.count_ready.side_effect = slow_ready
            opened = stack.enter_context(mock.patch.object(wb, 'open_upstream', side_effect=[wb.RateLimited(None, 'quota'), (None, None, None)]))
            with self.assertRaises(TimeoutError): wb.open_upstream_with_fallback(BODY, target_realm='intl', deadline_at=105.)
            self.assertEqual(opened.call_count, 1)

    def test_explicit_budget_prebuilt_policy_and_exclusions_survive_both_exits(self):
        with ExitStack() as stack:
            self.enable(stack)
            opened = stack.enter_context(mock.patch.object(wb, 'open_upstream', side_effect=[wb.RateLimited(None, 'quota'), (None, None, None)]))
            deadline = time.monotonic() + 4
            prebuilt = {'messages': []}
            wb.open_upstream_with_fallback(BODY, target_realm='intl', prebuilt_body=prebuilt,
                deadline_at=deadline, allowed_accounts={'cn-first'}, exclude_uids={'used'})
            for c in opened.call_args_list:
                self.assertIs(c.kwargs['prebuilt_body'], prebuilt)
                self.assertEqual(c.kwargs['deadline_at'], deadline)
                self.assertEqual(c.kwargs['allowed_accounts'], {'cn-first'})
                self.assertEqual(c.kwargs['exclude_uids'], {'used'})

    def test_six_entry_paths_recover_on_actual_realm_and_preserve_attempt_billing(self):
        for api in APIS:
            with self.subTest(api=api), ExitStack() as stack:
                self.records.clear(); self.enable(stack)
                stack.enter_context(mock.patch.dict(os.environ, {'WB_OPS_LOOP_RETRY': '1'}))
                stack.enter_context(mock.patch.object(wb, 'CURRENT_REALM', 'intl'))
                a = Account('cn-first'); a.realm = 'cn'
                b = Account('cn-second'); b.realm = 'cn'
                first = ManagedSource([b''.join(turn({'reasoning_content': LOOP}, tokens=120))])
                second = ManagedSource([b''.join(turn({'content': 'recovered'}, tokens=130))])
                first._wb_billed_model = second._wb_billed_model = PAID
                opened = stack.enter_context(mock.patch.object(wb, 'open_upstream', side_effect=[wb.RateLimited(None, 'quota'), (first, a, 'high'), (second, b, 'high')]))
                _, out = self.dispatch(api)
                self.assertIn('recovered', out)
                self.assertEqual([c.kwargs['target_realm'] for c in opened.call_args_list], ['intl', 'cn', 'cn'])
                for c in opened.call_args_list:
                    self.assertEqual(c.kwargs['allowed_accounts'], {'cn-first', 'cn-second'})
                    self.assertIsNotNone(c.kwargs['deadline_at'])
                self.assertIn('cn-first', opened.call_args.kwargs['exclude_uids'])
                self.assertEqual([k['account'] for _, _, k in self.records], ['cn-first', 'cn-second'])
                self.assertEqual([args[0] for _, args, _ in self.records], [PAID, PAID])
                self.assertEqual(sum((args[1] if t == 'usage' else k['usage'])['total_tokens'] for t, args, k in self.records), 250)
                self.assertTrue(first.closed and second.closed)

    def test_first_frame_read_reopens_same_domestic_account_with_original_phase_budget(self):
        for api in APIS:
            with self.subTest(api=api), ExitStack() as stack:
                self.records.clear(); self.enable(stack)
                stack.enter_context(mock.patch.object(wb, 'CURRENT_REALM', 'intl'))
                stack.enter_context(mock.patch.object(wb.time, 'monotonic', return_value=100.))
                stack.enter_context(mock.patch.object(wb, 'upstream_timeouts', return_value=(120., 300.)))
                account = Account('cn-first'); account.realm = 'cn'
                first = Stream([], ConnectionResetError('synthetic reset'))
                second = ManagedSource([b''.join(turn({'content': 'recovered'}))])
                opened = stack.enter_context(mock.patch.object(wb, 'open_upstream', side_effect=[wb.RateLimited(None, 'quota'), (first, account, 'high'), (second, account, 'high')]))
                _, out = self.dispatch(api)
                self.assertIn('recovered', out)
                self.assertEqual([c.kwargs['target_realm'] for c in opened.call_args_list], ['intl', 'cn', 'cn'])
                self.assertEqual([c.kwargs['deadline_at'] for c in opened.call_args_list], [220., 220., 145.])
                self.assertIs(opened.call_args.kwargs['pin_account'], account)
                self.assertTrue(first.closed and second.closed)

    def test_six_entry_paths_never_replay_after_visible_content_or_native_tools(self):
        for api in APIS:
            for delta in ({'content': 'visible'}, {'tool_calls': [{'index': 0, 'id': 'native', 'function': {'name': 'lookup', 'arguments': '{}'}}]}):
                with self.subTest(api=api, delta=delta), ExitStack() as stack:
                    self.records.clear(); self.enable(stack)
                    stack.enter_context(mock.patch.object(wb, 'CURRENT_REALM', 'intl'))
                    account = Account('cn-first'); account.realm = 'cn'
                    raw = ManagedSource([b''.join(turn(delta, finish='error'))])
                    opened = stack.enter_context(mock.patch.object(wb, 'open_upstream', side_effect=[wb.RateLimited(None, 'quota'), (raw, account, 'high')]))
                    self.dispatch(api)
                    self.assertEqual(opened.call_count, 2)


if __name__ == '__main__':
    suite = unittest.TestSuite(RealmBoundaryTests(n) for n in RealmBoundaryTests.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

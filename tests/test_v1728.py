"""Synthetic cross-adapter termination and realm-policy regressions."""
import copy
import http.client
import json
import threading
import time
import unittest
from unittest import mock

from test_integration import Account, BODY, MODEL, Stream, chunk, turn, wb
from test_v1617 import MigrationTests
from test_v172 import DSML
from test_guarded_sse_batching import ChunkSource

APIS = ('chat', 'chat_json', 'responses', 'responses_json', 'messages', 'messages_json')


class AdapterTests(MigrationTests):
    def test_filtered_printed_calls_never_execute_six_paths(self):
        for api in APIS:
            for spelling in (DSML, DSML.replace('calls>', 'function_calls>')):
                with self.subTest(api=api, spelling=spelling[:25]), mock.patch.object(wb, 'open_upstream') as opened:
                    _, out = self.invoke_new(api, turn({'content': 'BEFORE ' + spelling + ' AFTER'}, 'content_filter'))
                    self.assertNotIn('"name": "inspect"', out)
                    self.assertIn('BEFORE ', out)
                    self.assertIn('DSML', out)
                    self.assertIn(' AFTER', out)
                    opened.assert_not_called()

    def test_responses_filtered_text_keeps_original_order(self):
        data = [chunk({'content': p}) for p in ('BEFORE ', DSML, ' AFTER')]
        h, _ = self.invoke_new('responses', data + turn({}, 'content_filter'))
        events = [json.loads(line[6:]) for line in b''.join(h.written).decode().splitlines() if line.startswith('data: {')]
        text = ''.join(e.get('delta', '') for e in events if e['type'] == 'response.output_text.delta')
        self.assertEqual(text, 'BEFORE ' + DSML + ' AFTER')
        self.assertEqual(events[-1]['type'], 'response.incomplete')

    def test_transport_error_flushes_printed_suffix_in_chat_and_responses(self):
        for api in ('chat', 'responses', 'messages'):
            with self.subTest(api=api):
                source = Stream([chunk({'content': p}) for p in ('BEFORE ', DSML, ' AFTER')], http.client.IncompleteRead(b''))
                holder = {'base_body': copy.deepcopy(BODY), 'account': Account('first')}
                gen = wb.stream_responses_events(source, MODEL, holder) if api == 'responses' else wb._stream_chat_events(source, MODEL, holder)
                if api == 'messages':
                    gen = wb.stream_messages_events(gen, MODEL)
                pieces, calls = [], []
                with self.assertRaises(http.client.IncompleteRead):
                    for raw in gen:
                        for line in raw.decode().splitlines():
                            if not line.startswith('data: {'):
                                continue
                            event = json.loads(line[6:])
                            if api == 'responses':
                                if event['type'] == 'response.output_text.delta': pieces.append(event['delta'])
                                if event['type'] == 'response.output_item.added' and event['item']['type'] == 'function_call': calls.append(event)
                            elif api == 'messages':
                                if event.get('type') == 'content_block_delta': pieces.append(event['delta'].get('text', ''))
                                if event.get('type') == 'content_block_start' and event['content_block']['type'] == 'tool_use': calls.append(event)
                            else:
                                for c in event.get('choices', []):
                                    pieces.append(c['delta'].get('content', ''))
                                    calls.extend(c['delta'].get('tool_calls', []))
                self.assertEqual(''.join(pieces), 'BEFORE ' + DSML + ' AFTER')
                self.assertFalse(calls)

    def test_recovery_freezes_allowlist_across_threads(self):
        allowed = {'first'}
        retry = wb._make_retry_call(BODY, 'session', 'intl', allowed_accounts=allowed)
        allowed.clear()
        with mock.patch.object(wb, 'open_upstream', return_value=(Stream([]), Account('first'), 'high')) as opened:
            thread = threading.Thread(target=retry, args=([],), kwargs={'deadline_at': 123})
            thread.start(); thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(opened.call_args.kwargs['allowed_accounts'], {'first'})
        self.assertEqual(opened.call_args.kwargs['deadline_at'], 123)

    def test_fallback_and_read_recovery_keep_actual_realm_policy_and_budget(self):
        account = Account('allowed-cn'); account.realm = 'cn'
        source = Stream(turn({'content': 'OK'}))
        pool = mock.Mock(); pool.count_ready.return_value = 1
        seen = []
        def opened(body, **kwargs):
            seen.append(kwargs)
            if kwargs['target_realm'] == 'intl':
                raise wb.RateLimited(None, 'quota', wait=60)
            return source, account, 'high'
        def probe(raw, retry_call, **kwargs):
            retry_call([], deadline_at=kwargs['deadline_at'])
            return raw, account
        with mock.patch.object(wb, 'POOL', pool), mock.patch.object(wb, 'realm_fallback_enabled', return_value=True), \
             mock.patch.object(wb, 'restriction_hold_remaining', return_value=0), \
             mock.patch.object(wb, 'open_upstream', side_effect=opened), \
             mock.patch.object(wb, 'probe_first_frame', side_effect=probe):
            response, actual, _ = wb._open_first_stream(BODY, realm='intl', allowed_accounts={'allowed-cn'}, allow_realm_fallback=True)
        self.assertIs(actual, account)
        self.assertEqual([r['target_realm'] for r in seen], ['intl', 'cn', 'cn'])
        self.assertEqual(seen[0]['deadline_at'], seen[1]['deadline_at'])
        self.assertTrue(all(r['deadline_at'] <= response._ops_generation_deadline for r in seen))
        self.assertTrue(all(r['allowed_accounts'] == {'allowed-cn'} for r in seen))
        self.assertIs(seen[2]['pin_account'], account)
        pool.count_ready.assert_called_once_with(realm='cn', model=MODEL, allowed=frozenset({'allowed-cn'}))

    def test_fallback_rejects_pin_restriction_hold_and_expired_budget(self):
        for scenario in ('pin', 'restriction', 'expired', 'explicit'):
            error = wb.RateLimited(None, 'unavailable', wait=60)
            with self.subTest(scenario=scenario), mock.patch.object(wb, 'realm_fallback_enabled', return_value=True), \
                 mock.patch.object(wb, 'restriction_hold_remaining', return_value=60 if scenario == 'restriction' else 0), \
                 mock.patch.object(wb, 'realm_fallback_target', return_value='cn'), \
                 mock.patch.object(wb, 'open_upstream', side_effect=error) as opened:
                with self.assertRaises(wb.RateLimited):
                    wb.open_upstream_with_fallback(BODY, target_realm='intl',
                        allow_realm_fallback=scenario != 'explicit',
                        pin_account=Account() if scenario == 'pin' else None,
                        deadline_at=time.monotonic() + (-1 if scenario == 'expired' else 5))
                self.assertEqual(opened.call_count, 1)

    def test_adapter_compatibility_never_discards_account_restrictions(self):
        def legacy(body, session_key=None, target_realm=None):
            self.fail('restricted request reached an adapter without policy support')
        with mock.patch.object(wb, 'open_upstream', legacy):
            for policy in ({'allowed_accounts': {'first'}}, {'pin_account': Account()}):
                with self.assertRaises(TypeError): wb._open_with_compat(BODY, **policy)


if __name__ == '__main__':
    suite = unittest.TestSuite(AdapterTests(n) for n in AdapterTests.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

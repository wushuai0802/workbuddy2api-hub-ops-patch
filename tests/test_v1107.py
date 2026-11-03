"""Synthetic v1.10.7 contracts adapted to this repository's owned fixtures.

The reference archive was read as data; none of its scripts are invoked.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from test_account_slot_lifecycle import AccountSlotTests, APIS
from test_guarded_sse_batching import ChunkSource
from test_integration import Account, BODY, MODEL, REAL_RECORD, turn, wb
from test_loop_recovery import LOOP
from test_v1100 import V1100Tests, REAL_ERROR

C = wb.wb_codearts


class V1107Tests(AccountSlotTests):
    def test_connection_keeps_original_proxy_snapshot_without_credentials(self):
        with ExitStack() as stack:
            account, opened, _, raw = self.context(stack)
            account.proxy_slot = 'original'
            account.proxy = 'http://synthetic:password@proxy.invalid:8080'
            def connect(*args, **kwargs):
                account.proxy_slot = 'changed'
                account.proxy = 'http://changed.invalid:80'
                return raw
            opened.side_effect = connect
            out, _, _ = wb.open_upstream(BODY, allowed_accounts={account.uid})
            self.assertEqual(out._wb_account_uid, account.uid)
            self.assertEqual(out._ops_proxy_slot, 'original')
            self.assertEqual(out._ops_proxy_exit, 'proxy.invalid:8080')
            out.close()
            self.assertEqual(wb.account_inflight_snapshot(), {})

    def test_connection_failure_preserves_account_and_exit_once(self):
        with ExitStack() as stack:
            account, opened, _, _ = self.context(stack)
            account.proxy_slot = 'failed'
            account.proxy = 'http://synthetic:password@proxy.invalid:8080'
            opened.side_effect = ValueError('synthetic connection failure')
            with self.assertRaises(ValueError) as caught:
                wb.open_upstream(BODY, allowed_accounts={account.uid})
            wb.upstream_failure_response(wb_fixture_handler(), MODEL, caught.exception, time.time(), 'key')
            self.assertEqual(len(self.records), 1)
            fields = self.records[0][2]
            self.assertEqual(fields['account'], account.uid)
            self.assertEqual(fields['proxy_slot'], 'failed')
            self.assertEqual(fields['proxy_exit'], 'proxy.invalid:8080')
            self.assertEqual(wb.account_inflight_snapshot(), {})
            # 403/429 wrappers must carry the exact failed HTTP attempt too.
            for wrapper in (wb.ContentRejected, wb.RateLimited):
                self.records.clear()
                wrapped = wrapper(caught.exception, 'synthetic upstream rejection')
                wb._record_open_error(wrapped, MODEL, 403, 'synthetic', account=None)
                self.assertEqual(len(self.records), 1)
                fields = self.records[0][2]
                self.assertEqual(fields['account'], account.uid)
                self.assertEqual(fields['proxy_slot'], 'failed')
                self.assertEqual(fields['proxy_exit'], 'proxy.invalid:8080')

    def test_all_six_adapters_keep_success_and_error_provenance(self):
        for api in APIS:
            for finish in ('stop', 'error'):
                self.records.clear()
                raw = ChunkSource([b''.join(turn({'content': 'answer'}, finish, 29))])
                raw._ops_proxy_slot = 'attempt'
                raw._ops_proxy_exit = 'exit.invalid:99'
                self.invoke(api, raw, Account('first'))
                self.assertEqual(len(self.records), 1, api)
                kind, args, fields = self.records[0]
                self.assertEqual(fields['proxy_slot'], 'attempt')
                self.assertEqual(fields['proxy_exit'], 'exit.invalid:99')
                self.assertEqual((args[1] if kind == 'usage' else fields['usage'])['total_tokens'], 29)
                self.assertTrue(raw.closed)

    def test_recovery_records_both_accounts_and_clears_previous_exit(self):
        with mock.patch.dict(os.environ, {'WB_OPS_LOOP_RETRY': '1'}):
            for api in APIS:
                self.records.clear()
                first = ChunkSource([b''.join(turn({'reasoning_content': LOOP}, tokens=120))])
                second = ChunkSource([b''.join(turn({'content': 'recovered'}, tokens=130))])
                first._ops_proxy_slot = 'old'
                first._ops_proxy_exit = 'old.invalid:1'
                with mock.patch.object(wb, 'open_upstream', return_value=(second, Account('second'), 'high')):
                    output = self.invoke(api, first, Account('first'))
                self.assertIn('recovered', output, api)
                self.assertEqual(len(self.records), 2, api)
                self.assertEqual(self.records[0][2]['proxy_slot'], 'old')
                self.assertNotIn('proxy_slot', self.records[1][2])
                self.assertNotIn('proxy_exit', self.records[1][2])
                self.assertEqual([r[2]['account'] for r in self.records], ['first', 'second'])
                self.assertEqual(sum((a[1] if k == 'usage' else f['usage'])['total_tokens']
                                     for k, a, f in self.records), 250)

    def test_codearts_same_uid_never_queries_workbuddy_pool(self):
        for api in APIS:
            for finish in ('stop', 'error'):
                source, account, pool = V1100Tests.source(self, turn({'content': 'answer'}, finish, 29))
                workbuddy_pool = mock.Mock()
                rows = []
                with mock.patch.object(wb, 'POOL', workbuddy_pool), \
                     mock.patch.object(wb, 'record_usage', REAL_RECORD), \
                     mock.patch.object(wb, 'record_error', REAL_ERROR), \
                     mock.patch.object(wb, '_persist_usage', side_effect=lambda row, _: rows.append(row)):
                    V1100Tests.invoke(self, api, source, account)
                workbuddy_pool.get.assert_not_called()
                pool.release.assert_called_once_with(account)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['realm'], 'codearts')
                self.assertEqual(rows[0]['account'], account.uid)
                self.assertNotIn('proxy_slot', rows[0])

    def test_native_flash_catalog_and_chat_routing_remain_realm_scoped(self):
        for force in (False, True):
            with mock.patch.object(wb.wb_settings, 'deepseek_flash_force_sg', return_value=force):
                for realm in ('intl', 'cn'):
                    body = {'model': MODEL}
                    wb._apply_deepseek_flash_target(body, realm)
                    self.assertEqual(body['model'], MODEL + '-sg' if force and realm == 'intl' else MODEL)
                    entry = wb.model_entry(MODEL, {'credits': 'x0.11'}, realm)
                    self.assertEqual(entry['credits'], 'x0.00' if realm == 'intl' and not force else 'x0.11')

    def test_media_defaults_closed_and_local_references_rejected(self):
        config = wb.wb_settings.media_config(wb.ACCOUNTS_DIR)
        self.assertFalse(config['enabled'])
        handler = object.__new__(wb.Handler)
        with mock.patch.object(wb.wb_media_client, 'generate_image') as generate:
            with self.assertRaises(wb.wb_media.MediaError) as caught:
                handler._media_prepare('/v1/images/generations', {}, config)
        self.assertEqual(caught.exception.code, 'NO_ROUTE')
        generate.assert_not_called()
        with mock.patch.object(wb.wb_media_client.socket, 'getaddrinfo', side_effect=AssertionError('no DNS')):
            for url in ('http://127.0.0.1/a', 'http://169.254.169.254/a', 'http://10.0.0.1/a', 'file:///etc/passwd'):
                with self.assertRaises(wb.wb_media.MediaError):
                    wb.wb_media_client.assert_public_url(url)

    def test_media_quota_denial_or_exception_returns_exactly_one_slot(self):
        handler = object.__new__(wb.Handler)
        handler.key_entry = {'id': 'media-key'}
        handler._media_config = lambda: {'enabled': True, 'image_max_per_minute': 0, 'daily_credits_per_key': 1}
        handler._media_prepare = lambda *args: {'model': {'id': 'synthetic-image'}, 'realm': 'cn'}
        handler._media_error = mock.Mock(return_value='denied')
        handler._pick_media_account = mock.Mock()
        self.assertEqual(wb.media_inflight(), 0)
        self.addCleanup(lambda: [wb.release_media_slot() for _ in range(wb.media_inflight())])
        with mock.patch.object(wb, 'media_rate_allow', return_value=(True, 0)), \
             mock.patch.object(wb, 'media_image_cost_hint', return_value=5), \
             mock.patch.object(wb, 'media_key_spend_allow', return_value=(False, 'quota')) as gate:
            for _ in range(3):
                self.assertEqual(handler._handle_media_post('/v1/images/generations', {}), 'denied')
                self.assertEqual(wb.media_inflight(), 0)
            gate.side_effect = ValueError('synthetic quota lookup')
            with self.assertRaises(ValueError):
                handler._handle_media_post('/v1/images/generations', {})
            self.assertEqual(wb.media_inflight(), 0)
        handler._pick_media_account.assert_not_called()

    def test_ledger_bridge_preserves_large_unicode_rows_across_threads_and_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'usage.jsonl')
            script = ('import json,sys,wb_usage_io\nfor i in range(30):\n'
                      ' wb_usage_io.append_line(sys.argv[1],json.dumps({"id":sys.argv[2]+str(i),"text":"汉"*6000},ensure_ascii=False))\n')
            with mock.patch.object(wb, 'USAGE_LOG', path), \
                 mock.patch.object(wb.wb_usage_io, 'append_line', wraps=wb.wb_usage_io.append_line) as append:
                processes = [subprocess.Popen([sys.executable, '-B', '-c', script, path, f'p{p}-'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE) for p in range(2)]
                def writer(prefix):
                    for n in range(30):
                        wb._persist_usage({'id': prefix + str(n), 'text': '汉' * 6000}, 'failed')
                threads = [threading.Thread(target=writer, args=(f't{n}-',)) for n in range(3)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(20)
                    self.assertFalse(thread.is_alive())
                for process in processes:
                    try:
                        _, error = process.communicate(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                        raise
                    self.assertEqual(process.returncode, 0, error)
                self.assertEqual(append.call_count, 90)
            rows = [json.loads(line) for line in Path(path).read_text(encoding='utf8').splitlines()]
            self.assertEqual(len(rows), 150)
            self.assertEqual(len({row['id'] for row in rows}), 150)
            self.assertTrue(all(row['text'] == '汉' * 6000 for row in rows))

    def test_ledger_failure_is_diagnostic_without_raising(self):
        with mock.patch.object(wb.wb_usage_io.os, 'open', side_effect=OSError('synthetic disk failure')), \
             mock.patch.object(wb, 'log') as log:
            wb._persist_usage({'id': 'failed'}, 'ledger failed')
        self.assertEqual(log.call_count, 1)
        self.assertIn('synthetic disk failure', str(log.call_args))

    def test_busy_codearts_preserves_whitelist_and_other_requests_slot(self):
        pool = mock.Mock()
        pool.accounts = [SimpleNamespace(active_concurrent=1, disabled=False, cool_until=0)]
        pool.pick.return_value = None
        with mock.patch.object(C, 'pool_for', return_value=pool), \
             mock.patch.object(C.time, 'sleep') as sleep, mock.patch.object(C, '_client_chat') as connect:
            with self.assertRaises(C.CodeArtsUnavailable):
                C.open_chat({'enabled': True, 'accounts_dir': 'synthetic'}, BODY, allowed_accounts={'allowed'})
        self.assertEqual(pool.accounts[0].active_concurrent, 1)
        pool.acquire.assert_not_called()
        pool.release.assert_not_called()
        pool.note_error.assert_not_called()
        connect.assert_not_called()
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(call.kwargs['allowed_accounts'] == {'allowed'} for call in pool.pick.call_args_list))

    def test_codearts_autoclaim_defaults_off_and_has_native_attempt_cap(self):
        account = SimpleNamespace(uid='synthetic', disabled=False, checkin={})
        pool = SimpleNamespace(accounts_dir=wb.ACCOUNTS_DIR, accounts=[account], config={'credits_auto_claim': False})
        key = os.path.abspath(pool.accounts_dir)
        self.addCleanup(C._AUTO_CHECKIN_STATE.pop, key, None)
        with mock.patch.object(C.threading, 'Thread') as thread:
            C._maybe_auto_checkin(pool)
            thread.assert_not_called()
            pool.config['credits_auto_claim'] = True
            start = time.time()
            for n in range(C.AUTO_CHECKIN_MAX_ATTEMPTS + 2):
                with mock.patch.object(C.time, 'time', return_value=start + n * (C.AUTO_CHECKIN_RETRY_GAP + 1)):
                    C._maybe_auto_checkin(pool)
            self.assertEqual(thread.call_count, C.AUTO_CHECKIN_MAX_ATTEMPTS)
        self.assertEqual(C._AUTO_CHECKIN_STATE[key][account.uid][1], C.AUTO_CHECKIN_MAX_ATTEMPTS)


def wb_fixture_handler():
    from test_integration import Handler
    return Handler()


if __name__ == '__main__':
    suite = unittest.TestSuite(V1107Tests(name) for name in V1107Tests.__dict__ if name.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

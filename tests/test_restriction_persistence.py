"""Restriction waves survive restarts without extending expiry or exposing accounts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from test_integration import wb


class RestrictionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ['WB_OPS_TEST_STATE'])))
        self.enterContext(mock.patch.object(wb,'USAGE_DIR',str(self.root)))
        self.enterContext(mock.patch.object(wb,'log'))
        self.enterContext(mock.patch.multiple(wb,RESTRICTION_WAVE_MIN=2,RESTRICTION_WAVE_WINDOW=900.,RESTRICTION_HOLD_SECONDS=1800.))
        wb.reset_restriction_state()
        self.addCleanup(wb.reset_restriction_state)

    def child(self,code):
        source='import json,wb_proxy as w; w.USAGE_DIR='+repr(str(self.root))+'; '+code
        out=subprocess.check_output([sys.executable,'-B','-c',source],text=True,timeout=15)
        return json.loads(out.splitlines()[-1])

    def test_active_pause_survives_fresh_process_without_extension(self):
        wb.note_account_restriction('intl','PRIVATE_ACCOUNT_A',now=1000)
        self.assertEqual(wb.note_account_restriction('intl','PRIVATE_ACCOUNT_B',now=1010),2810)
        self.assertEqual(self.child("print(json.dumps([w.restriction_hold_remaining('intl',now=1015),w.restriction_hold_remaining('cn',now=1015),w.restriction_hold_remaining('intl',now=2811)]))"),[1795,0,0])

    def test_threshold_events_survive_restart_and_same_account_is_not_counted_twice(self):
        wb.note_account_restriction('intl','PRIVATE_ACCOUNT_A',now=1000)
        self.assertEqual(self.child("print(json.dumps([w.note_account_restriction('intl','PRIVATE_ACCOUNT_A',now=1010),w.note_account_restriction('intl','PRIVATE_ACCOUNT_B',now=1020)]))"),[0,2820])
        self.assertEqual(self.child("print(json.dumps(w.restriction_hold_remaining('intl',now=1025)))"),1795)

    def test_other_account_after_restart_triggers_original_window(self):
        wb.note_account_restriction('cn','PRIVATE_ACCOUNT_A',now=1000)
        self.assertEqual(self.child("print(json.dumps(w.note_account_restriction('cn','PRIVATE_ACCOUNT_B',now=1010)))"),2810)
        state=(self.root/'restriction-state.json').read_text()
        self.assertNotIn('PRIVATE_',state)
        self.assertNotIn('accessToken',state)

    def test_explicit_reset_is_persistent_and_does_not_touch_account_data(self):
        sentinel=self.root/'account-fixture.json';sentinel.write_text('preserve-current')
        wb.note_account_restriction('intl','A',now=1000);wb.note_account_restriction('intl','B',now=1010)
        wb.reset_restriction_state()
        self.assertEqual(self.child("print(json.dumps(w.restriction_hold_remaining('intl',now=1015)))"),0)
        self.assertEqual(sentinel.read_text(),'preserve-current')

    def test_write_failure_keeps_in_memory_pause(self):
        with mock.patch.object(wb.os,'replace',side_effect=OSError('synthetic disk failure')):
            wb.note_account_restriction('intl','A',now=1000);wb.note_account_restriction('intl','B',now=1010)
        self.assertEqual(wb.restriction_hold_remaining('intl',now=1015),1795)
        self.assertGreater(wb.restriction_state_snapshot(now=1015)['write_failures'],0)

    def test_corrupt_store_is_reported_and_does_not_invent_a_hold(self):
        (self.root/'restriction-state.json').write_text('{broken')
        result=self.child("print(json.dumps(w.restriction_state_snapshot(now=1015)))")
        self.assertTrue(result['load_error'])
        self.assertEqual(result['realms']['intl']['remaining_seconds'],0)

    def test_explicit_off_switch_remains_effective_after_restart(self):
        wb.note_account_restriction('intl','A',now=1000);wb.note_account_restriction('intl','B',now=1010)
        self.assertEqual(self.child("w.RESTRICTION_HOLD_SECONDS=0; print(json.dumps(w.restriction_hold_remaining('intl',now=1015)))"),0)

    def test_store_rejects_symlink_without_reading_or_overwriting_target(self):
        path=self.root/'restriction-state.json';path.unlink()
        target=self.root/'preserved-fixture';target.write_text('PRIVATE_SENTINEL')
        try:path.symlink_to(target)
        except OSError as exc:self.skipTest('symlink unavailable: '+str(exc))
        result=self.child("print(json.dumps(w.restriction_state_snapshot(now=1015)))")
        self.assertTrue(result['load_error'])
        wb.note_account_restriction('intl','A',now=1000)
        self.assertEqual(target.read_text(),'PRIVATE_SENTINEL')

    def test_oversized_and_recursive_state_never_crashes_dispatch_check(self):
        path=self.root/'restriction-state.json'
        for raw in ('['*2000+']'*2000,'x'*(1024*1024+1),
                    '{"schema":1,"holds":{"intl":'+('9'*1000)+'},"events":{}}'):
            path.write_text(raw)
            result=self.child("print(json.dumps(w.restriction_state_snapshot(now=1015)))")
            self.assertTrue(result['load_error'])
            self.assertEqual(result['realms']['intl']['remaining_seconds'],0)

    def test_parallel_distinct_accounts_preserve_atomic_state(self):
        start=threading.Barrier(6);errors=[]
        def note(index):
            try:
                start.wait(2)
                wb.note_account_restriction('intl','PRIVATE_ACCOUNT_'+str(index),now=1000)
            except BaseException as exc:errors.append(exc)
        threads=[threading.Thread(target=note,args=(i,))for i in range(6)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3)
        self.assertFalse(any(thread.is_alive()for thread in threads))
        self.assertEqual(errors,[])
        self.assertEqual(self.child("print(json.dumps(w.restriction_hold_remaining('intl',now=1015)))"),1785)
        data=json.loads((self.root/'restriction-state.json').read_text())
        self.assertEqual(len(data['events']['intl']),6)


if __name__=='__main__':unittest.main()

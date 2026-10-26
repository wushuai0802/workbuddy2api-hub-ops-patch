"""Scheduled/manual daily activity must stop and persist verified account restrictions."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
import urllib.error
from test_integration import MODEL,wb


class TaskRestrictionTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ['WB_OPS_TEST_STATE'])))
        (self.root/'accounts').mkdir();(self.root/'usage').mkdir()
        self.enterContext(mock.patch.object(wb,'USAGE_DIR',str(self.root/'usage')))
        self.enterContext(mock.patch.object(wb,'log'))
        self.enterContext(mock.patch.object(wb.wb_accounts.wb_activity,'record_attempt'))
        self.enterContext(mock.patch.multiple(wb,RESTRICTION_WAVE_MIN=2,RESTRICTION_WAVE_WINDOW=900.,RESTRICTION_HOLD_SECONDS=1800.))
        wb.reset_restriction_state();self.addCleanup(wb.reset_restriction_state)

    def account(self,uid='task-A'):
        a=wb.wb_accounts.Account({'uid':uid,'realm':'intl','accessToken':'PRIVATE_ACCESS','refreshToken':'PRIVATE_REFRESH'})
        a.path=str(self.root/'accounts'/(uid+'.json'))
        a.save(str(self.root/'accounts'))
        a.headers=mock.Mock(return_value={'Authorization':'Bearer synthetic'})
        a.daily_chat_model=mock.Mock(return_value=MODEL)
        a.fetch_credits=mock.Mock()
        return a

    def error(self,code=11140,status=403):
        error=urllib.error.HTTPError('https://example.invalid/chat',status,'synthetic',{},io.BytesIO(json.dumps({'code':code,'msg':'synthetic rejection'}).encode()))
        self.addCleanup(error.close)
        return error

    def test_desktop_restriction_persists_and_next_attempt_sends_nothing(self):
        a=self.account()
        with mock.patch.object(wb.wb_accounts,'urlopen',side_effect=lambda *a,**k:(_ for _ in ()).throw(self.error()))as opened:
            result=a.daily_chat(web=False)
            self.assertFalse(result['ok'])
            self.assertGreater(a.restricted_until,time.time()+7100)
            again=a.daily_chat(web=False)
            self.assertFalse(again['ok']);self.assertTrue(again['blocked'])
            self.assertNotIn('skipped',again)
            self.assertEqual(opened.call_count,1)
        saved=json.loads(Path(a.path).read_text())
        self.assertEqual(saved['accessToken'],'PRIVATE_ACCESS')
        self.assertEqual(saved['refreshToken'],'PRIVATE_REFRESH')
        self.assertGreater(saved['restrictedUntil'],time.time()+7100)
        self.assertFalse(a.last_daily_chat)
        a.fetch_credits.assert_not_called()

    def test_two_restricted_accounts_stop_third_account_in_same_batch(self):
        accounts=[self.account('task-'+str(i))for i in range(3)]
        with mock.patch.object(wb.wb_accounts,'urlopen',side_effect=lambda *a,**k:(_ for _ in ()).throw(self.error()))as opened:
            results=[a.daily_chat(web=False)for a in accounts]
        self.assertTrue(all(not r['ok']for r in results))
        self.assertEqual(opened.call_count,2)
        self.assertTrue(results[-1]['blocked'])
        self.assertGreater(wb.restriction_hold_remaining('intl'),1700)

    def test_other_rejections_and_code_prefixes_do_not_cool_account(self):
        self.assertFalse(wb.restriction_is_account_level('{"code":111400}'))
        for code,status in ((11134,403),(111400,403),(6004,429)):
            a=self.account('task-'+str(code))
            with mock.patch.object(wb.wb_accounts,'urlopen',side_effect=self.error(code,status)):
                self.assertFalse(a.daily_chat(web=False)['ok'])
            self.assertEqual(a.restricted_until,0)

    def test_web_restriction_stops_rounds_without_marking_daily_complete(self):
        a=self.account()
        a.web_conversation_reclaim=mock.Mock();a.web_conversation_sweep_orphans=mock.Mock()
        a.web_fallback_models=mock.Mock(return_value=['model-a','model-b'])
        a.daily_chat_web_policy=mock.Mock(return_value=(30,2));a.daily_chat_budget=mock.Mock(return_value=120)
        a._web_retry_pause=mock.Mock()
        a._daily_chat_web_once=mock.Mock(return_value={'ok':False,'retryable':True,'error':'HTTP 403: {"code":11140}'})
        result=a.daily_chat_web()
        self.assertFalse(result['ok']);self.assertFalse(result['retryable'])
        self.assertEqual(a._daily_chat_web_once.call_count,1)
        a._web_retry_pause.assert_not_called()
        self.assertGreater(a.restricted_until,time.time()+7100)
        self.assertFalse(a.last_daily_chat)

    def test_active_region_pause_blocks_direct_web_entry_before_cleanup(self):
        wb.note_account_restriction('intl','prior-a');wb.note_account_restriction('intl','prior-b')
        a=self.account()
        a._daily_chat_web_upstream=mock.Mock(return_value={'ok':True})
        result=a.daily_chat_web()
        self.assertFalse(result['ok']);self.assertTrue(result['blocked'])
        a._daily_chat_web_upstream.assert_not_called()

    def test_daily_task_uses_live_main_module_pause(self):
        a=self.account()
        main=types.SimpleNamespace(restriction_hold_remaining=mock.Mock(return_value=120),note_account_restriction=mock.Mock())
        with mock.patch.dict(sys.modules,{'__main__':main}),mock.patch.object(wb.wb_accounts,'urlopen',side_effect=self.error())as opened:
            result=a.daily_chat(web=False)
        self.assertFalse(result['ok']);self.assertTrue(result.get('blocked'))
        opened.assert_not_called()
        main.restriction_hold_remaining.assert_called_once_with('intl')

    def test_web_quota_wording_does_not_become_account_restriction(self):
        a=self.account()
        a.web_conversation_reclaim=mock.Mock();a.web_conversation_sweep_orphans=mock.Mock()
        a.web_fallback_models=mock.Mock(return_value=['model-a'])
        a.daily_chat_web_policy=mock.Mock(return_value=(30,2));a.daily_chat_budget=mock.Mock(return_value=120)
        a._web_retry_pause=mock.Mock()
        a._daily_chat_web_once=mock.Mock(return_value={'ok':False,'retryable':True,'error':'HTTP 429: {"code":6004,"msg":"已被限制调用频率"}'})
        result=a.daily_chat_web()
        self.assertFalse(result['ok'])
        self.assertEqual(a.restricted_until,0)
        self.assertEqual(a._daily_chat_web_once.call_count,2)


if __name__=='__main__':unittest.main()

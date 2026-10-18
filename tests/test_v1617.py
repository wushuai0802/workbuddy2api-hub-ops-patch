"""New-baseline contracts: instruction preservation, six adapters and accounting."""
import copy
import gzip
import io
import json
import os
import time
import unittest
import urllib.error
from unittest import mock
from test_integration import IntegrationTests, Handler, Account, Stream, BODY, MODEL, chunk, turn, wb
from test_loop_recovery import GOOD_CALL
from test_nullable_buffer import frames, NULL_SHAPES

REAL_ERROR = wb.record_error


class MigrationTests(IntegrationTests):
    def invoke_new(self, api, data, body=None):
        h = Handler('/v1/messages' if api.startswith('messages') else '/v1/chat/completions')
        h.key_entry = {'id': 'synthetic-key-id'}
        args = (Stream(data), MODEL)
        if api.startswith('responses'):
            args += (set(), {})
        args += ({'hash': 'synthetic'}, Account('first'), time.time())
        method = {'chat': '_chat_stream_response', 'chat_json': '_chat_nonstream_response',
                  'responses': '_responses_stream_response', 'responses_json': '_responses_nonstream_response',
                  'messages': '_messages_stream_response', 'messages_json': '_messages_nonstream_response'}[api]
        getattr(h, method)(*args, base_body=copy.deepcopy(body or BODY), effort='high')
        return h, json.dumps(h.json_body) if h.json_body is not None else b''.join(h.written).decode()

    def test_instruction_passthrough_even_if_degrade_state_is_active(self):
        messages = [{'role':'system','content':'Project instructions'},
                    {'role':'developer','content':'Tool constraints'}, {'role':'user','content':'Task'}]
        with mock.patch.dict(os.environ, {'WB_OPS_ALLOW_PROMPT_DEGRADE':'0'}), \
                mock.patch.object(wb, 'prompt_mode_config', return_value={'mode':'passthrough'}), \
                mock.patch.object(wb.PROMPT_DEGRADE, 'active', return_value=True):
            self.assertEqual(wb.apply_prompt_mode(messages), messages)
        with mock.patch.dict(os.environ, {'WB_OPS_ALLOW_PROMPT_DEGRADE':'1'}), \
                mock.patch.object(wb, 'prompt_mode_config', return_value={'mode':'passthrough'}), \
                mock.patch.object(wb.PROMPT_DEGRADE, 'active', return_value=True):
            self.assertNotEqual(wb.apply_prompt_mode(messages), messages)

    def test_default_http403_never_changes_instructions_or_retries(self):
        account = mock.Mock(uid='synthetic', realm='intl', proxy=None)
        account.headers.return_value = {}
        account.chat_base_url.return_value = 'https://example.invalid'
        pool = mock.Mock(); pool.count_ready.return_value = 1
        pool.pick_for_session.return_value = account
        payload = dict(BODY, messages=[{'role':'system','content':'KEEP_RULE'},*BODY['messages']])
        with mock.patch.dict(os.environ, {'WB_OPS_ALLOW_PROMPT_DEGRADE':'0'}), \
                mock.patch.object(wb, 'POOL', pool), \
                mock.patch.object(wb, 'prompt_cache_key_enabled', return_value=False), \
                mock.patch.object(wb, 'prompt_mode_config', return_value={'mode':'passthrough'}), \
                mock.patch.object(wb.PROMPT_DEGRADE, 'trigger') as trigger, \
                mock.patch.object(wb.wb_accounts, 'urlopen', side_effect=urllib.error.HTTPError(
                    'https://example.invalid',403,'denied',{},io.BytesIO(b'denied'))) as opened:
            with self.assertRaises(wb.ContentRejected): wb.open_upstream(payload)
            self.assertEqual(opened.call_count,1); trigger.assert_not_called()
            raw = opened.call_args.args[0].data
            if raw.startswith(b'\x1f\x8b'): raw = gzip.decompress(raw)
            self.assertIn('KEEP_RULE', raw.decode())

    def test_public_upstream_triple_preserves_effort_success_and_timeout(self):
        account = mock.Mock(uid='synthetic', realm='intl', proxy=None)
        account.headers.return_value = {};account.chat_base_url.return_value='https://example.invalid'
        pool=mock.Mock();pool.count_ready.return_value=1;pool.pick_for_session.return_value=account
        stream=Stream(turn({'content':'answer'}))
        with mock.patch.object(wb,'POOL',pool), mock.patch.object(wb,'upstream_timeouts',return_value=(45.,77.)), \
                mock.patch.object(wb,'_apply_stream_idle_timeout') as idle, \
                mock.patch.object(wb.wb_accounts,'urlopen',return_value=stream) as opened:
            actual=wb.open_upstream(dict(BODY,reasoning_effort='high'), deadline_at=time.monotonic()+.5)
            self.assertEqual(actual,(stream,account,'high'));account.note_success.assert_called_once_with(model=MODEL)
            self.assertLessEqual(opened.call_args.kwargs['timeout'],.5);idle.assert_called_once_with(stream,77.)

    def test_accounting_keeps_key_and_effort_all_six_paths(self):
        for api in ('chat','chat_json','responses','responses_json','messages','messages_json'):
            self.records.clear()
            h,out=self.invoke_new(api,turn({'content':'answer'},tokens=26))
            self.assertIn('answer',out)
            rows=[r for r in self.records if r[0]=='usage']
            self.assertEqual(len(rows),1,(api,self.records))
            self.assertEqual(rows[0][1][1]['total_tokens'],26)
            self.assertEqual(rows[0][2]['key'],'synthetic-key-id');self.assertEqual(rows[0][2]['effort'],'high')

    def test_native_triple_recovery_keeps_key_effort_and_per_attempt_usage(self):
        for api in ('chat','chat_json','responses','responses_json','messages','messages_json'):
            self.records.clear()
            with mock.patch.object(wb,'open_upstream',return_value=(Stream(turn({'content':'answer'},tokens=26)),Account('second'),'high')) as opened:
                h,out=self.invoke_new(api,turn({'reasoning_content':'unfinished'},tokens=14))
            self.assertEqual(opened.call_count,1);self.assertIn('answer',out)
            rows=[r for r in self.records if r[0]=='usage']
            self.assertEqual([r[1][1]['total_tokens'] for r in rows],[14,26],api)
            self.assertEqual([r[2]['account'] for r in rows],['first','second'],api)
            self.assertTrue(all(r[2]['key']=='synthetic-key-id' and r[2]['effort']=='high' for r in rows))

    def test_real_error_record_persists_optional_effort(self):
        with mock.patch.object(wb,'_usage',dict(wb._usage)), \
                mock.patch.object(wb,'_persist_usage') as persist:
            row=REAL_ERROR(MODEL,502,'synthetic failure',usage={'total_tokens':14},
                           key='synthetic-key-id',effort='high')
            persist.assert_called_once_with(row,'error persist failed')
            self.assertEqual(row['key'],'synthetic-key-id')
            self.assertEqual(row['reasoning_effort'],'high')
            self.assertTrue(row['error'])
            self.assertNotIn('reasoning_effort',REAL_ERROR(MODEL,502,'legacy caller'))

    def test_failed_recovery_does_not_double_charge_and_retains_identity(self):
        for api in ('chat','chat_json','responses','responses_json','messages','messages_json'):
            self.records.clear()
            with mock.patch.object(wb,'open_upstream',side_effect=OSError('synthetic connect failure')) as opened:
                _,out=self.invoke_new(api,turn({'reasoning_content':'unfinished'},tokens=14))
            self.assertEqual(opened.call_count,1)
            self.assertIn('continuation_request_failed',out)
            total=0
            for kind,args,kwargs in self.records:
                usage=args[1] if kind=='usage' else kwargs.get('usage')
                total+=(usage or {}).get('total_tokens',0)
                self.assertEqual(kwargs.get('key'),'synthetic-key-id',api)
                self.assertEqual(kwargs.get('effort'),'high',api)
            self.assertEqual(total,14,(api,self.records))

    def test_messages_loop_recovers_once_and_double_loop_fails(self):
        with mock.patch.dict(os.environ,{'WB_OPS_LOOP_RETRY':'1'}):
            for api in ('messages','messages_json'):
                for success in (True,False):
                    retry=turn({'content':'answer'}) if success else frames(NULL_SHAPES[0],128)
                    with mock.patch.object(wb,'open_upstream',return_value=(Stream(retry),Account('second'),'high')) as opened:
                        h,out=self.invoke_new(api,frames(NULL_SHAPES[0],128))
                    self.assertEqual(opened.call_count,1,(api,out))
                    self.assertIn('answer' if success else 'reasoning_loop_detected',out)
                    if not success and api=='messages':self.assertNotIn('event: message_stop',out)

    def test_messages_visible_text_and_tools_block_loop_retry(self):
        with mock.patch.dict(os.environ,{'WB_OPS_LOOP_RETRY':'1'}):
            for api in ('messages','messages_json'):
                for visible in ({'content':'visible'},GOOD_CALL):
                    with mock.patch.object(wb,'open_upstream') as opened:
                        _,out=self.invoke_new(api,[chunk(visible)]+frames(NULL_SHAPES[0],128))
                    opened.assert_not_called();self.assertIn('reasoning_loop_detected',out)

    def test_messages_tools_usage_and_length_termination(self):
        for api in ('messages','messages_json'):
            _,out=self.invoke_new(api,turn(GOOD_CALL,'tool_calls'))
            self.assertIn('tool_use',out);self.assertIn('lookup',out)
            _,out=self.invoke_new(api,turn({'content':'partial'},'length'))
            self.assertIn('max_tokens',out)

    def test_messages_abrupt_eof_is_error_not_success(self):
        for api in ('messages','messages_json'):
            _,out=self.invoke_new(api,[chunk({'content':'partial'})])
            self.assertIn('upstream_truncated',out)
            if api=='messages':self.assertNotIn('event: message_stop',out)

    def test_official_pairing_repair_and_truncation_filter_remain(self):
        base=copy.deepcopy(BODY)
        base['messages'] += [{'role':'assistant','tool_calls':[{'id':i,'type':'function','function':{'name':'lookup','arguments':'{}'}}]} for i in ('a','b')]
        base['messages'] += [{'role':'tool','tool_call_id':i,'content':'ok'} for i in ('a','b')]
        result=wb.build_upstream_body(base)
        self.assertTrue(any(len(m.get('tool_calls',[]))==2 for m in result['messages']))
        bad=copy.deepcopy(GOOD_CALL);bad['tool_calls'][0]['function']['arguments']='{"broken":'
        for api in ('chat_json','responses_json','messages_json'):
            _,out=self.invoke_new(api,turn(bad,'length'))
            self.assertNotIn('"name": "lookup"',out,api)

if __name__=='__main__':
    suite=unittest.TestSuite(MigrationTests(n) for n in MigrationTests.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

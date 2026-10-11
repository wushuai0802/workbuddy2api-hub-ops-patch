"""Offline opt-in contracts; simulated answers do not establish effectiveness."""
import copy
import json
import os
import unittest
from unittest import mock
from test_metadata_buffer import MetadataBufferTests, META, ROLE
from test_nullable_buffer import NULL_SHAPES, frames
from test_integration import Account, BODY, Stream, turn, wb
from test_loop_recovery import GOOD_CALL

class RecoveryHintTests(MetadataBufferTests):
    def test_default_retry_body_is_unchanged_all_modes(self):
        for api in ('chat','responses','chat_json','responses_json'):
            body=copy.deepcopy(BODY);before=copy.deepcopy(body)
            with mock.patch.dict(os.environ,{'WB_OPS_LOOP_RECOVERY_HINT':'0'}),mock.patch.object(wb,'open_upstream',return_value=(Stream(turn({'content':'answer'})),Account('b'))) as opened:
                h=self.invoke(api,Stream([META,ROLE]+frames(NULL_SHAPES[0],128)),body=body)
            self.assertIn('answer',self.output(h));self.assertEqual(opened.call_count,1)
            self.assertEqual(opened.call_args.args[0],before);self.assertEqual(body,before)
            self.assertNotIn('retry_hint_applied',self.counts())

    def test_hint_only_affects_one_detached_recovery_body(self):
        for api in ('chat','responses','chat_json','responses_json'):
            self.clear_events();body=copy.deepcopy(BODY)
            body.update(thinking={'type':'enabled'},reasoning_effort='high',max_tokens=65536)
            body['tools']=[{'type':'function','function':{'name':'lookup','parameters':{'type':'object'}}}]
            before=copy.deepcopy(body)
            with mock.patch.dict(os.environ,{'WB_OPS_LOOP_RECOVERY_HINT':'1'}),mock.patch.object(wb,'open_upstream',return_value=(Stream(turn(GOOD_CALL,'tool_calls')),Account('b'))) as opened:
                h=self.invoke(api,Stream([META,ROLE]+frames(NULL_SHAPES[0],128)),body=body)
            self.assertIn('lookup',self.output(h));self.assertEqual(opened.call_count,1)
            actual=opened.call_args.args[0]
            self.assertEqual(actual['messages'][:-1],before['messages'])
            self.assertEqual(actual['messages'][-1]['role'],'user')
            self.assertEqual({k:v for k,v in actual.items() if k!='messages'},{k:v for k,v in before.items() if k!='messages'})
            self.assertEqual(body,before);self.assertEqual(self.counts().get('retry_hint_applied'),1)

    def test_tool_history_is_preserved_and_unpaired_tools_skip_hint(self):
        body=copy.deepcopy(BODY)
        body['messages'] += [{'role':'assistant','reasoning_content':'Evidence.','tool_calls':GOOD_CALL['tool_calls']},
                             {'role':'tool','tool_call_id':'call_good','content':'Found result'}]
        before=copy.deepcopy(body);result=wb._loop_recovery_body(body)
        self.assertEqual(result['messages'][:-1],before['messages']);self.assertEqual(body,before)
        for messages in (body['messages'][:-1],[{'role':'tool','tool_call_id':'unmatched'}],
                         [{'role':'assistant','function_call':{'name':'legacy'}}],[],['invalid']):
            bad=dict(body,messages=messages)
            self.assertIsNone(wb._loop_recovery_body(bad))
            with mock.patch.object(wb,'open_upstream',return_value=(None,None)) as opened:
                wb._make_retry_call(bad,None,None)([],recovery_hint=True)
            self.assertIs(opened.call_args.args[0],bad)

    def test_success_content_loop_and_tool_fragments_do_not_apply_hint(self):
        with mock.patch.dict(os.environ,{'WB_OPS_LOOP_RECOVERY_HINT':'1'}):
            for api in ('chat','responses','chat_json','responses_json'):
                for data in (turn({'content':'answer'}),frames({'content':'visible'},128),
                             [wb._chat_frame({'model':BODY['model']},GOOD_CALL)]+frames(NULL_SHAPES[0],128)):
                    with mock.patch.object(wb,'open_upstream') as opened:self.invoke(api,Stream(data))
                    opened.assert_not_called()
        self.assertNotIn('retry_hint_applied',self.counts())

    def test_failed_hint_has_no_extra_retry(self):
        with mock.patch.dict(os.environ,{'WB_OPS_LOOP_RECOVERY_HINT':'1'}):
            for api in ('chat','responses','chat_json','responses_json'):
                with mock.patch.object(wb,'open_upstream',return_value=(Stream([META]+frames(NULL_SHAPES[0],128)),Account('b'))) as opened:
                    h=self.invoke(api,Stream([META]+frames(NULL_SHAPES[0],128)))
                self.assertEqual(opened.call_count,1);self.assertIn('reasoning_loop_detected',self.output(h))

    def test_empty_turn_retry_and_initial_callback_ignore_opt_in(self):
        with mock.patch.dict(os.environ,{'WB_OPS_LOOP_RECOVERY_HINT':'1'}):
            body=copy.deepcopy(BODY)
            with mock.patch.object(wb,'open_upstream',return_value=(Stream(turn({'content':'answer'})),Account('b'))) as opened:
                h=self.invoke('chat',Stream(turn({'reasoning_content':'Unfinished analysis.'})),body=body)
            self.assertEqual(opened.call_count,1);self.assertEqual(opened.call_args.args[0],body)
            with mock.patch.object(wb,'open_upstream',return_value=(None,None)) as opened:
                wb._make_retry_call(body,'session','intl')([],deadline_at=123)
            self.assertIs(opened.call_args.args[0],body)
            self.assertEqual(opened.call_args.kwargs['deadline_at'],123)
            self.assertNotIn('retry_hint_applied',self.counts())

if __name__=='__main__':
    suite=unittest.TestSuite(RecoveryHintTests(n) for n in RecoveryHintTests.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

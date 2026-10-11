"""Health-observed null extension and shipped-version identity regressions."""
import json
from pathlib import Path
import unittest
from unittest import mock
from test_nullable_buffer import NullableBufferTests, frames
from test_metadata_buffer import META, HEART
from test_integration import Account, Stream, chunk, turn, wb
from test_loop_recovery import GOOD_CALL

WIRE={'role':'assistant','content':'','function_call':None,'refusal':'','tool_calls':[],'extra_fields':None}
MODES=('chat','responses','chat_json','responses_json')

class ExtraFieldsTests(NullableBufferTests):
    def test_observed_null_extension_is_bufferable_and_unchanged(self):
        delta=dict(WIRE,reasoning_content='Distinct evidence supports the result.')
        detail={}
        self.assertIsNone(wb._reasoning_buffer_boundary(json.loads(chunk(delta).decode()[6:]),detail))
        self.assertEqual(detail,{})
        data=[META,chunk(WIRE),HEART,chunk(delta)]+turn({'content':'answer','extra_fields':None})
        self.assertEqual(self.raw(data),data)
        self.assertEqual(self.counts().get('reasoning_buffer_entered'),1)
        self.assertNotIn('reasoning_buffer_skip_protocol',self.counts())

    def test_loop_discards_buffer_and_recovers_all_modes(self):
        for size in (17,128,1024):
            for api in MODES:
                self.clear_events();before=len(self.records)
                with mock.patch.object(wb,'open_upstream',return_value=(Stream(turn({'content':'answer','extra_fields':None})),Account('b'))) as opened:
                    h=self.invoke(api,Stream([META,chunk(WIRE),chunk(dict(WIRE,reasoning_content='Evidence from first attempt.'))]+frames(WIRE,size)))
                self.assertEqual(opened.call_count,1)
                self.assertIn('answer',self.output(h));self.assertNotIn('Let me run',self.output(h))
                self.assertEqual(self.counts().get('reasoning_buffer_discarded'),1)
                self.assertEqual(self.counts().get('retry_recovered'),1)
                self.assertNotIn('reasoning_buffer_skip_protocol',self.counts())
                usage=[r for r in self.records[before:] if r[0]=='usage']
                self.assertTrue(usage);self.assertTrue(all(r[1][1].get('completion_tokens',0)>0 for r in usage))

    def test_double_loop_remains_explicit_failure_with_one_retry(self):
        for api in MODES:
            self.clear_events()
            with mock.patch.object(wb,'open_upstream',return_value=(Stream([META]+frames(WIRE,128)),Account('b'))) as opened:
                h=self.invoke(api,Stream([META]+frames(WIRE,128)))
            self.assertEqual(opened.call_count,1)
            self.assertIn('reasoning_loop_detected',self.output(h));self.assertNotIn('Let me run',self.output(h))
            self.assertEqual(self.counts().get('reasoning_buffer_discarded'),2)

    def test_null_does_not_change_normal_answers_or_tools(self):
        for api in MODES:
            for final,finish in (({'content':'answer'},'stop'),(GOOD_CALL,'tool_calls')):
                data=[META]+frames(WIRE,text='Important evidence.')+turn(dict(final,extra_fields=None),finish)
                with mock.patch.object(wb,'open_upstream') as opened:h=self.invoke(api,Stream(data))
                opened.assert_not_called();self.assertIn('Important evidence.',self.thought(h))
                self.assertIn('answer' if finish=='stop' else 'lookup',self.output(h))

    def test_non_null_values_including_empty_are_not_whitelisted(self):
        for value in ('',False,0,[],{},'PRIVATE_SECRET',{'PRIVATE_KEY':'PRIVATE_SECRET'}):
            self.clear_events();delta=dict(WIRE,extra_fields=value,reasoning_content='Evidence.')
            detail={};self.assertEqual(wb._reasoning_buffer_boundary(json.loads(chunk(delta).decode()[6:]),detail),'protocol')
            self.assertEqual(detail['reason'],'extra_fields_value')
            data=[chunk(delta)]+turn({'content':'answer'})
            self.assertEqual(self.raw(data),data)
            report=json.dumps(wb.loop_health_snapshot())
            self.assertNotIn('PRIVATE_SECRET',report);self.assertNotIn('PRIVATE_KEY',report)
            self.assertEqual(self.counts().get('reasoning_buffer_protocol_extra_fields_value'),1)

    def test_other_or_nested_unknown_fields_are_still_rejected(self):
        for delta in (dict(WIRE,other_extension=None),
                      dict(WIRE,tool_calls=[{'index':0,'extra_fields':None}])):
            detail={};self.assertEqual(wb._reasoning_buffer_boundary(json.loads(chunk(delta).decode()[6:]),detail),'protocol')

    def test_real_output_with_null_extension_still_blocks_replay(self):
        for api in MODES:
            for boundary in ({'content':'visible'},GOOD_CALL,{'tool_calls':[{'index':0,'function':{'arguments':'{'}}]}):
                data=[META]+frames(WIRE,text='Evidence.')+[chunk(dict(boundary,extra_fields=None))]+frames(WIRE,128)
                with mock.patch.object(wb,'open_upstream') as opened:self.invoke(api,Stream(data))
                opened.assert_not_called()

    def test_health_version_equals_bundle_installation_id(self):
        meta=json.loads((Path(__file__).resolve().parents[1]/'BASELINE.json').read_text())
        self.assertEqual(wb.retry_health_snapshot()['ops_patch_id'],meta['patch_id'])

if __name__=='__main__':
    suite=unittest.TestSuite(ExtraFieldsTests(n) for n in ExtraFieldsTests.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

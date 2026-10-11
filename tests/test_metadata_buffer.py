"""State-machine regressions for complete SSE preambles and interleaved frames."""
import json
import unittest
from unittest import mock
from test_nullable_buffer import NullableBufferTests, NULL_SHAPES, frames
from test_integration import Account, MODEL, Stream, chunk, turn, wb

META=b'data: {"choices":[],"usage":null}\n\n'
HEART=b': keep-alive\n\n'
ROLE=chunk({'role':'assistant','content':None})

class MetadataBufferTests(NullableBufferTests):
    def test_preambles_do_not_disable_buffer_all_apis(self):
        for preamble in ([META],[HEART],[META,ROLE,HEART,b'\n']):
            for shape in NULL_SHAPES:
                for api in ('chat','responses','chat_json','responses_json'):
                    self.clear_events()
                    with mock.patch.object(wb,'open_upstream',return_value=(Stream(turn({'content':'answer'})),Account('b'))) as opened:
                        h=self.invoke(api,Stream(preamble+frames(shape,128)))
                    self.assertEqual(opened.call_count,1)
                    self.assertIn('answer',self.output(h));self.assertNotIn('Let me run',self.output(h))
                    self.assertEqual(self.counts().get('reasoning_buffer_discarded'),1)

    def test_preamble_delay_does_not_consume_thinking_budget(self):
        data=[META,ROLE,HEART]+frames(NULL_SHAPES[0],text='Useful evidence.')
        with mock.patch.object(wb.time,'monotonic',side_effect=[0,20,50,90]):
            self.assertEqual(self.raw(data),data)
        self.assertEqual(self.counts().get('reasoning_buffer_entered'),1)
        self.assertNotIn('reasoning_buffer_skip_time',self.counts())

    def test_interleaved_metadata_preserves_order_and_eligibility(self):
        data=[META,ROLE]+frames(NULL_SHAPES[0],text='First thought.')+[HEART,META]+frames(NULL_SHAPES[1],text='Second thought.')+turn({'content':'answer'})
        actual=self.raw(data)
        # Comments are delivered promptly while thought is withheld; data
        # frames retain their order. The actual metadata still releases it.
        self.assertEqual([x for x in actual if x != HEART],[x for x in data if x != HEART])
        self.assertLess(actual.index(HEART),actual.index(data[2]))
        self.assertEqual(self.counts().get('reasoning_buffer_entered'),2)
        self.assertEqual(self.counts().get('reasoning_buffer_release_metadata'),1)
        self.assertEqual(self.counts().get('reasoning_buffer_release_content'),1)

    def test_metadata_never_reenables_exhausted_time_budget(self):
        data=frames(NULL_SHAPES[0],text='First.')+frames(NULL_SHAPES[0],text='Second.')+[META]+frames(NULL_SHAPES[0],text='Third.')
        with mock.patch.object(wb.time,'monotonic',side_effect=[0,3,4,5]):
            self.assertEqual(self.raw(data),data)
        self.assertEqual(self.counts().get('reasoning_buffer_entered'),1)
        self.assertEqual(self.counts().get('reasoning_buffer_release_time'),1)

    def test_content_and_tool_boundaries_still_block_replay_after_metadata(self):
        for delta in ({'content':'answer'},{'tool_calls':[{'id':'call_x','index':0,'function':{'arguments':'{'}}]}):
            for api in ('chat','responses','chat_json','responses_json'):
                with mock.patch.object(wb,'open_upstream') as opened:
                    self.invoke(api,Stream([META,ROLE]+frames(NULL_SHAPES[0],text='Evidence.')+[chunk(delta),META]+frames(NULL_SHAPES[0])))
                opened.assert_not_called()

    def test_unknown_protocol_passes_unchanged_with_private_safe_detail(self):
        delta={'reasoning_content':'Evidence.','unknown_extension':{'PRIVATE_VALUE':'secret'},'PRIVATE-DYNAMIC-ID':123}
        data=[META,chunk(delta)]+turn({'content':'answer'})
        self.assertEqual(self.raw(data),data)
        report=wb.loop_health_snapshot();dump=json.dumps(report)
        self.assertIn('delta_fields',dump);self.assertIn('unknown_extension',dump)
        for secret in ('Evidence.','PRIVATE_VALUE','secret','PRIVATE-DYNAMIC-ID'):
            self.assertNotIn(secret,dump)
        event=next(e for e in report['recent'] if e['kind']=='reasoning_buffer_protocol_detail')
        self.assertEqual(event['shape']['count'],3)
        self.assertEqual(event['shape']['fields'][1]['type'],'dict')

    def test_protocol_categories_are_fixed_and_cardinality_bounded(self):
        cases=[({'choices':'wrong'},'choices_shape'),({'choices':[{'delta':'wrong'}]},'delta_type'),
               ({'choices':[{'delta':{'role':'PRIVATE_ROLE'}}]},'role_value'),
               ({'choices':[{'delta':{'reasoning_content':[]}}]},'reasoning_type'),
               ({'choices':[],'private_extra':'SECRET'},'metadata_fields')]
        for frame,reason in cases:
            detail={};self.assertEqual(wb._reasoning_buffer_boundary(frame,detail),'protocol')
            self.assertEqual(detail['reason'],reason)
            self.assertNotIn('SECRET',json.dumps(detail));self.assertNotIn('PRIVATE_ROLE',json.dumps(detail))
        self.raw([chunk({'unknown_'+str(i):'PRIVATE_VALUE'}) for i in range(100)])
        self.assertEqual(self.counts().get('reasoning_buffer_protocol_delta_fields'),1)
        self.assertLessEqual(len(wb.loop_health_snapshot()['recent']),64)

    def test_done_invalid_json_and_real_eof_have_distinct_outcomes(self):
        data=frames(NULL_SHAPES[0],text='Thought.')+[b'data: [DONE]\n\n']
        self.assertEqual(self.raw(data),data)
        self.assertEqual(self.counts().get('reasoning_buffer_release_terminal'),1)
        self.clear_events();bad=b'data: {private malformed content\n\n'
        self.assertEqual(self.raw([bad]),[bad])
        report=json.dumps(wb.loop_health_snapshot())
        self.assertIn('invalid_json',report);self.assertNotIn('private malformed',report)

    def test_double_failure_with_metadata_has_no_third_generation(self):
        for api in ('chat','responses','chat_json','responses_json'):
            self.clear_events()
            with mock.patch.object(wb,'open_upstream',return_value=(Stream([META,ROLE]+frames(NULL_SHAPES[0],128)),Account('b'))) as opened:
                h=self.invoke(api,Stream([META,ROLE]+frames(NULL_SHAPES[0],128)))
            self.assertEqual(opened.call_count,1)
            self.assertIn('reasoning_loop_detected',self.output(h));self.assertNotIn('Let me run',self.output(h))
            self.assertEqual(self.counts().get('reasoning_buffer_discarded'),2)

if __name__=='__main__':
    suite=unittest.TestSuite(MetadataBufferTests(n) for n in MetadataBufferTests.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

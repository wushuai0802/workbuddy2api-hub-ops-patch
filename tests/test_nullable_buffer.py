"""Wire-shaped nullable SSE fields must not bypass bounded thinking buffers."""
import json
import os
import unittest
from unittest import mock

from test_dated_guards import DatedIntegrationTests
from test_integration import Account, BODY, MODEL, Stream, chunk, turn, responses, wb
from test_loop_recovery import LOOP, GOOD_CALL

NULL_SHAPES = [
    {'content': None},
    {'content': '', 'tool_calls': [], 'function_call': None, 'refusal': None},
    {'role': 'assistant', 'content': None, 'tool_calls': None, 'function_call': {}, 'refusal': ''},
    {'content': None, 'tool_calls': [{'index': 0, 'id': None, 'type': 'function',
                                    'function': {'name': '', 'arguments': ''}}],
     'function_call': {'name': None, 'arguments': None}},
]


def frames(extra, size=17, text=LOOP):
    return [chunk(dict(extra, reasoning_content=text[i:i+size])) for i in range(0,len(text),size)]


class NullableBufferTests(DatedIntegrationTests):
    def counts(self):
        return wb.loop_health_snapshot()['counts']

    def clear_events(self):
        with wb._LOOP_STATS_LOCK:
            wb._LOOP_STATS.clear();wb._LOOP_EVENTS.clear()

    def thought(self, handler):
        if handler.json_body is not None:
            body=handler.json_body
            if body.get('choices'):
                return body['choices'][0]['message'].get('reasoning_content','')
            return ''.join(p.get('text','') for item in body.get('output',[]) if item.get('type')=='reasoning' for p in item.get('summary',[]))
        return ''.join(''.join(c.get('delta',{}).get('reasoning_content') or '' for c in frame.get('choices',[]))
                       if 'choices' in frame else frame.get('delta','') if frame.get('type')=='response.reasoning_summary_text.delta' else ''
                       for frame in responses(handler))

    def raw(self, data, holder=None):
        holder=holder or {'model':MODEL,'generation_deadline':None,'base_body':BODY}
        stream=Stream(data);guard=wb._GuardedStream(stream,holder)
        try:return list(guard)
        finally:guard.close()

    def test_nullable_reasoning_loop_recovers_without_exposing_held_text(self):
        for shape in NULL_SHAPES:
            for size in (17,128):
                for api in ('chat','responses','chat_json','responses_json'):
                    with self.subTest(shape=shape,size=size,api=api):
                        self.clear_events()
                        with mock.patch.object(wb,'open_upstream',return_value=(Stream(turn({'content':'answer'})),Account('b'))) as opened:
                            h=self.invoke(api,Stream(frames(shape,size)))
                        self.assertEqual(opened.call_count,1)
                        self.assertNotIn('Let me run',self.output(h));self.assertIn('answer',self.output(h))
                        self.assertEqual(self.counts().get('reasoning_buffer_entered'),1)
                        self.assertEqual(self.counts().get('reasoning_buffer_discarded'),1)
                        self.assertEqual(self.counts().get('retry_recovered'),1)

    def test_nullable_double_failure_keeps_single_retry_and_usage(self):
        for api in ('chat','responses','chat_json','responses_json'):
            first=Stream(frames(NULL_SHAPES[1],128));second=Stream(frames(NULL_SHAPES[1],128))
            before=len(self.records)
            with mock.patch.object(wb,'open_upstream',return_value=(second,Account('b'))) as opened:
                h=self.invoke(api,first)
            self.assertEqual(opened.call_count,1)
            self.assertTrue(first.closed and second.closed)
            self.assertNotIn('Let me run',self.output(h))
            self.assertIn('reasoning_loop_detected',self.output(h))
            usage=[r for r in self.records[before:] if r[0]=='usage']
            # Failed terminal attempts use the existing error recorder, while
            # the replaced attempt's consumption must still be accounted for.
            self.assertTrue(usage)
            self.assertTrue(all(r[1][1].get('completion_tokens',0)>0 for r in usage))
            if api=='responses':self.assert_lifecycle(h,'response.failed')

    def test_nullable_normal_thought_text_and_valid_tools_are_preserved(self):
        for shape in NULL_SHAPES:
            for api in ('chat','responses','chat_json','responses_json'):
                for final,finish in (({'content':'answer'},'stop'),(GOOD_CALL,'tool_calls')):
                    with mock.patch.object(wb,'open_upstream') as opened:
                        h=self.invoke(api,Stream(frames(shape,text='Distinct evidence supports this conclusion.')+turn(final,finish)))
                    opened.assert_not_called()
                    self.assertIn('Distinct evidence supports this conclusion.',self.thought(h))
                    self.assertIn('answer' if finish=='stop' else 'lookup',self.output(h))

    def test_real_content_or_tool_fragments_flush_and_block_replay(self):
        cases=[({'content':'visible'},'content'),
               ({'tool_calls':[{'index':0,'id':'call_private'}]},'tool'),
               ({'tool_calls':[{'index':0,'function':{'name':'lookup'}}]},'tool'),
               ({'tool_calls':[{'index':0,'function':{'arguments':'{'}}]},'tool'),
               ({'function_call':{'arguments':'{'}},'tool')]
        for delta,boundary in cases:
            for api in ('chat','responses','chat_json','responses_json'):
                self.clear_events()
                data=frames(NULL_SHAPES[0],text='Prior valid evidence.')+[chunk(dict(delta,reasoning_content='Further evidence.'))]+frames(NULL_SHAPES[0])
                with mock.patch.object(wb,'open_upstream') as opened:h=self.invoke(api,Stream(data))
                opened.assert_not_called()
                self.assertEqual(self.counts().get('reasoning_buffer_release_'+boundary),1)
                if api in ('chat','responses'):
                    self.assertIn('Prior valid evidence.',self.thought(h))

    def test_raw_valid_frames_are_byte_exact_including_empty_placeholders(self):
        for shape in NULL_SHAPES:
            data=frames(shape,text='One ordinary thought.')+turn({'content':'answer'})
            self.assertEqual(self.raw(data),data)

    def test_real_usage_is_kept_when_buffered_failure_is_discarded(self):
        usage={'prompt_tokens':12,'completion_tokens':31,'total_tokens':43}
        data=frames(NULL_SHAPES[1],128)
        data[0]=chunk(dict(NULL_SHAPES[1],reasoning_content=LOOP[:128]),usage=usage)
        with mock.patch.object(wb,'open_upstream',return_value=(Stream(turn({'content':'answer'})),Account('b'))):
            self.invoke('chat',Stream(data))
        first=[r for r in self.records if r[0]=='usage' and r[2].get('account')=='a1']
        self.assertEqual(first[0][1][1],usage)

    def test_metadata_is_passed_and_release_reason_is_counted(self):
        data=frames(NULL_SHAPES[0],text='Thought.')+[b'data: {"choices":[],"usage":{"total_tokens":12}}\n\n']+turn({'content':'answer'})
        self.assertEqual(self.raw(data),data)
        self.assertEqual(self.counts().get('reasoning_buffer_release_metadata'),1)

    def test_refusal_unknown_fields_and_terminal_never_stay_buffered(self):
        for delta,reason in (({'refusal':'Cannot comply.'},'refusal'),({'future_extension':'nonempty'},'protocol')):
            self.clear_events()
            data=frames(NULL_SHAPES[0],text='Thought.')+[chunk(delta)]+turn({'content':'answer'})
            self.assertEqual(self.raw(data),data)
            self.assertEqual(self.counts().get('reasoning_buffer_release_'+reason),1)
        self.clear_events();data=frames(NULL_SHAPES[0],text='Thought.')+[chunk(finish='stop')]
        self.assertEqual(self.raw(data),data)
        self.assertEqual(self.counts().get('reasoning_buffer_release_terminal'),1)

    def test_buffer_boundaries_are_observable_and_normal_frames_are_unchanged(self):
        text=''.join('Distinct observation %d value %d.\n'%(i,i*i) for i in range(500))
        large=json.loads(chunk({'reasoning_content':'Evidence.'}).decode()[6:]);large['id']='a'*33000
        variants=[('chars',[chunk({'reasoning_content':text[i:i+4000]}) for i in range(0,len(text),4000)]),
                  ('bytes',[('data: '+json.dumps(large)+'\n\n').encode()]*3),
                  ('frames',[chunk({'reasoning_content':str(i)+' '}) for i in range(514)])]
        for reason,data in variants:
            self.clear_events()
            self.assertEqual(self.raw(data),data)
            self.assertEqual(self.counts().get('reasoning_buffer_release_'+reason),1,reason)

    def test_time_release_disabled_and_eof_have_fixed_categories(self):
        data=frames(NULL_SHAPES[0],text='Thought.')
        with mock.patch.dict(os.environ,{'WB_OPS_REASONING_BUFFER':'0'}):self.assertEqual(self.raw(data),data)
        self.assertEqual(self.counts().get('reasoning_buffer_skip_disabled'),1)
        self.clear_events();self.assertEqual(self.raw(data),data)
        self.assertEqual(self.counts().get('reasoning_buffer_release_eof'),1)
        self.clear_events()
        two=data+[chunk({'reasoning_content':'Next.','content':None})]
        with mock.patch.object(wb.time,'monotonic',side_effect=[0,3]):self.assertEqual(self.raw(two),two)
        self.assertEqual(self.counts().get('reasoning_buffer_release_time'),1)

    def test_transport_exception_flushes_partial_thinking_before_raising(self):
        data=frames(NULL_SHAPES[0],text='Evidence.')
        stream=Stream(data,error=OSError('synthetic disconnect'))
        guard=wb._GuardedStream(stream,{'model':MODEL,'generation_deadline':None})
        iterator=iter(guard)
        self.assertEqual(next(iterator),data[0])
        with self.assertRaises(OSError):next(iterator)
        guard.close()
        self.assertEqual(self.counts().get('reasoning_buffer_release_transport_error'),1)

    def test_buffer_counters_are_bounded_without_private_values(self):
        token='PRIVATE_TEST_TOOL_NAME'
        data=[chunk({'reasoning_content':'Thought.'})]+[chunk({'tool_calls':[{'function':{'name':token}}]})]*100
        self.raw(data)
        report=wb.loop_health_snapshot()
        self.assertLessEqual(len(report['recent']),64)
        self.assertNotIn(token,json.dumps(report))
        self.assertEqual(report['counts'].get('reasoning_buffer_skip_tool'),1)

    def test_malformed_boundary_shapes_do_not_qualify_as_pure_reasoning(self):
        for value in (None,[],{'choices':'invalid'}, {'choices':[{'delta':'invalid'}]},
                      {'choices':[{'delta':{'content':[]}}]}, {'choices':[{'delta':{'reasoning_content':[]}}]},
                      {'choices':[{'delta':{'tool_calls':{}}}]}):
            self.assertIsNotNone(wb._reasoning_buffer_boundary(value))


if __name__=='__main__':
    suite=unittest.TestSuite(NullableBufferTests(n) for n in NullableBufferTests.__dict__ if n.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())

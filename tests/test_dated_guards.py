"""Dated protections: partition stability, leak view, buffering and retry scope."""
import copy
import json
import os
import random
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import test_integration as integration
from test_loop_recovery import LOOP, GOOD_CALL, loop_frames
from test_integration import Account, BODY, Handler, MODEL, Stream, chunk, responses, turn, wb
from wb_ops import LeakedThinkingGuard, LoopRetryCircuit, ReasoningLoopGuard


class PartitionTests(unittest.TestCase):
    def result(self, text, sizes, conservative=False):
        g = ReasoningLoopGuard(conservative=conservative)
        offset = 0
        for size in sizes:
            if g.feed(text[offset:offset+size]):
                break
            offset += size
        return g.hit, g.rule, g.total_chars

    def test_exact_position_independent_of_partition(self):
        for text in (LOOP, LOOP.replace('\n', ' '), "Review a distinct issue.\n" + LOOP,
                     "```\n"+LOOP+"```\n"+LOOP, "\x1b[31mThinking:\x1b[0m\n"+LOOP):
            for content in (False, True):
                expected = self.result(text, [1]*len(text), content)
                self.assertTrue(expected[0])
                for size in (2, 17, 64, 128, 1024, len(text)):
                    self.assertEqual(self.result(text, [size]*(len(text)//size+1), content), expected)
                rng = random.Random(31)
                self.assertEqual(self.result(text, [rng.randint(1,131) for _ in text], content), expected)

    def test_leak_tag_split_and_literal_quotes(self):
        for size in (1, 2, 17, 1024):
            for tag in ('<thinking>', '  <THINK>', '<think>'):
                g = LeakedThinkingGuard(); text = tag + LOOP
                for i in range(0,len(text),size):
                    if g.feed(text[i:i+size]):break
                self.assertTrue(g.detected)
                self.assertTrue(g.guard.hit)
            for prefix in ('Example: <thinking>', '```\n<thinking>', '> <thinking>', '"<thinking>'):
                g = LeakedThinkingGuard();g.feed(prefix+LOOP)
                self.assertFalse(g.detected)

    def test_leak_closing_boundary_stops_special_view(self):
        for size in (1,17,1024):
            g=LeakedThinkingGuard();text='<thinking>Analyze the evidence.\n</thinking>'+LOOP
            for i in range(0,len(text),size):g.feed(text[i:i+size])
            self.assertTrue(g.detected);self.assertEqual(g.state,'done');self.assertFalse(g.guard.hit)

    def test_code_inside_leak_stays_excluded(self):
        g=LeakedThinkingGuard();g.feed('<thinking>```\n'+LOOP+'</thinking>\n```\n')
        self.assertFalse(g.finish())

    def test_circuit_expiry_bounded_atomic_and_hash_only(self):
        clock=[1.0];c=LoopRetryCircuit(ttl=120,limit=2,clock=lambda:clock[0])
        self.assertTrue(c.allow('one'));self.assertFalse(c.allow('one'))
        c.success('one');self.assertTrue(c.allow('one'))
        clock[0]=122;self.assertTrue(c.allow('one'))
        c.allow('two');c.allow('three');self.assertEqual(len(c.entries),2)
        c=LoopRetryCircuit()
        with ThreadPoolExecutor(8) as executor:
            self.assertEqual(sum(executor.map(c.allow,['same']*20)),1)
        self.assertTrue(c.allow(None))


class DatedIntegrationTests(integration.IntegrationTests):
    # Reuse assertions/setup, not the inherited r1/r2 test methods.
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {
            'WB_OPS_LOOP_RETRY':'1','WB_OPS_REASONING_BUFFER':'1',
            'WB_OPS_LOOP_CIRCUIT':'1','WB_OPS_LEAKED_THINKING_GUARD':'1'}))
        self.enterContext(mock.patch.object(wb,'_LOOP_CIRCUIT',LoopRetryCircuit()))
        with wb._LOOP_STATS_LOCK:wb._LOOP_STATS.clear();wb._LOOP_EVENTS.clear()

    def invoke(self,api,stream,body=BODY,session=None,owner=None):
        h=Handler();h.key_entry=owner
        opts=dict(base_body=body,session_key=session)
        if api=='chat':h._chat_stream_response(stream,MODEL,{},Account(),time.time(),**opts)
        elif api=='responses':h._responses_stream_response(stream,MODEL,set(),{}, {},Account(),time.time(),**opts)
        elif api=='chat_json':h._chat_nonstream_response(stream,MODEL,{},Account(),time.time(),**opts)
        else:h._responses_nonstream_response(stream,MODEL,set(),{}, {},Account(),time.time(),**opts)
        return h

    def output(self,h):
        return json.dumps(h.json_body) if h.json_body is not None else b''.join(h.written).decode()

    def test_buffered_loop_removed_but_usage_and_answer_preserved(self):
        for api in ('chat','responses','chat_json','responses_json'):
            with mock.patch.object(wb,'open_upstream',return_value=(Stream(turn({'reasoning_content':'Fresh analysis.','content':'answer'})),Account('b'))) as opened:
                h=self.invoke(api,Stream(loop_frames(128)))
            self.assertEqual(opened.call_count,1)
            out=self.output(h)
            self.assertNotIn('Let me run',out)
            self.assertIn('Fresh analysis.',out);self.assertIn('answer',out)
            self.assertNotIn('reasoning_loop_detected',out)
            self.assertTrue(any((r[1][1] or {}).get('completion_tokens',0)>0 for r in self.records if r[0]=='usage'))

    def test_double_loop_one_terminal_failure_no_hidden_extra_request(self):
        for api in ('chat','responses','chat_json','responses_json'):
            with mock.patch.object(wb,'open_upstream',return_value=(Stream(loop_frames(128)),Account('b'))) as opened:
                h=self.invoke(api,Stream(loop_frames(128)))
            self.assertEqual(opened.call_count,1)
            self.assertIn('reasoning_loop_detected',self.output(h))
            self.assertNotIn('Let me run',self.output(h))
            if api=='responses':self.assert_lifecycle(h,'response.failed')

    def test_one_large_loop_frame_still_records_its_usage(self):
        with mock.patch.object(wb,'open_upstream',return_value=(Stream(turn({'content':'answer'})),Account('b'))):
            h=self.invoke('chat',Stream([chunk({'reasoning_content':LOOP})]))
        self.assertIn('answer',self.output(h))
        first=[r for r in self.records if r[0]=='usage' and r[2].get('account')=='a1']
        self.assertEqual(len(first),1)
        self.assertGreater(first[0][1][1]['completion_tokens'],len(LOOP)//4)

    def test_valid_buffered_thought_and_tools_are_unchanged(self):
        for api in ('chat','responses','chat_json','responses_json'):
            frames=[chunk({'reasoning_content':'Evidence points to a dependency.'})]+turn(GOOD_CALL,'tool_calls')
            with mock.patch.object(wb,'open_upstream') as opened:h=self.invoke(api,Stream(frames))
            opened.assert_not_called()
            self.assertIn('Evidence points to a dependency.',self.output(h))
            self.assertIn('lookup',self.output(h))

    def test_leaked_content_normal_stop_and_tools_not_rewritten(self):
        text='<thinking>Consider the known dependency.\nThe conclusion follows.'
        for api in ('chat','responses','chat_json','responses_json'):
            with mock.patch.object(wb,'open_upstream') as opened:
                h=self.invoke(api,Stream(turn(dict(GOOD_CALL,content=text),'tool_calls')))
            opened.assert_not_called()
            self.assertIn('<thinking>',self.output(h));self.assertIn('lookup',self.output(h))
        self.assertGreater(wb.loop_health_snapshot()['counts'].get('thinking_in_content',0),0)

    def test_leaked_loop_fails_without_retry_or_promoting_answer(self):
        for api in ('chat','responses','chat_json','responses_json'):
            text='<thinking>'+LOOP
            frames=[chunk({'content':text[i:i+17]}) for i in range(0,len(text),17)]
            with mock.patch.object(wb,'open_upstream') as opened:h=self.invoke(api,Stream(frames))
            opened.assert_not_called();self.assertIn('content_loop_detected',self.output(h))

    def test_buffer_opt_out_and_cap(self):
        with mock.patch.dict(os.environ,{'WB_OPS_REASONING_BUFFER':'0'}),mock.patch.object(wb,'open_upstream',return_value=(Stream(turn({'content':'answer'})),Account('b'))):
            h=self.invoke('chat',Stream(loop_frames(128)))
        self.assertIn('Let me run',self.output(h))
        text=''.join('Distinct result %d has value %d.\n'%(i,i*i) for i in range(350))
        h=self.invoke('chat',Stream([chunk({'reasoning_content':text})]+turn({'content':'answer'})))
        self.assertIn('Distinct result 349',self.output(h))

    def test_buffer_time_cap_flushes_on_next_frame(self):
        holder={'model':MODEL,'base_body':BODY,'generation_deadline':None}
        stream=Stream([chunk({'reasoning_content':'First.'}),chunk({'reasoning_content':'Second.'})]+turn({'content':'answer'}))
        guarded=wb._GuardedStream(stream,holder)
        # Only the buffer uses monotonic in this guard because deadline=None.
        with mock.patch.object(wb.time,'monotonic',side_effect=[0,0,3,3,3,3,3,3]):
            iterator=iter(guarded);first=next(iterator)
            self.assertIn(b'First.',first)
            rest=list(iterator)
        guarded.close();self.assertTrue(any(b'Second.' in x for x in rest))

    def test_cross_request_same_context_cooldown_all_apis(self):
        for api in ('chat','responses','chat_json','responses_json'):
            with mock.patch.object(wb,'_LOOP_CIRCUIT',LoopRetryCircuit()),mock.patch.object(wb,'open_upstream',side_effect=lambda *a,**k:(Stream(loop_frames(128)),Account('b'))) as opened:
                h=self.invoke(api,Stream(loop_frames(128)),session='session')
                self.assertEqual(opened.call_count,1)
                h=self.invoke(api,Stream(loop_frames(128)),session='session')
                self.assertEqual(opened.call_count,1)
                self.assertIn('reasoning_loop_detected',self.output(h))
                changed=copy.deepcopy(BODY);changed['messages'].append({'role':'user','content':'new task'})
                self.invoke(api,Stream(loop_frames(128)),body=changed,session='session')
                self.assertEqual(opened.call_count,2)
                self.invoke(api,Stream(loop_frames(128)),session='session',owner={'id':'other-key'})
                self.assertEqual(opened.call_count,3)

    def test_later_suspicious_segment_can_buffer_after_initial_cap(self):
        text=''.join('New observation %d has evidence %d.\n'%(i,i*i) for i in range(350))
        with mock.patch.object(wb,'open_upstream',return_value=(Stream(turn({'content':'answer'})),Account('b'))):
            h=self.invoke('chat',Stream([chunk({'reasoning_content':text})]+loop_frames(17)))
        counts=wb.loop_health_snapshot()['counts']
        self.assertEqual(counts.get('reasoning_buffer_suspicion'),1)
        self.assertEqual(counts.get('reasoning_buffer_discarded'),1)
        self.assertIn('New observation 349',self.output(h))
        self.assertIn('answer',self.output(h))

    def test_initial_request_always_runs_and_success_resets_circuit(self):
        with mock.patch.object(wb,'open_upstream',side_effect=lambda *a,**k:(Stream(loop_frames(128)),Account('b'))) as opened:
            self.invoke('chat',Stream(loop_frames(128)),session='same')
            self.invoke('chat',Stream(turn({'content':'answer'})),session='same')
            self.invoke('chat',Stream(loop_frames(128)),session='same')
            self.assertEqual(opened.call_count,2)

    def test_no_session_or_disabled_circuit_does_not_guess_identity(self):
        for env,session in (({},None),({'WB_OPS_LOOP_CIRCUIT':'0'},'same')):
            with mock.patch.dict(os.environ,env),mock.patch.object(wb,'open_upstream',side_effect=lambda *a,**k:(Stream(loop_frames(128)),Account('b'))) as opened:
                for _ in range(2):self.invoke('chat',Stream(loop_frames(128)),session=session)
                self.assertEqual(opened.call_count,2)


if __name__=='__main__':
    suite=unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(PartitionTests))
    for name in DatedIntegrationTests.__dict__:
        if name.startswith('test_'):suite.addTest(DatedIntegrationTests(name))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())

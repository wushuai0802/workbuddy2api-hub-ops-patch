"""Independent phase budgets, bounded recovery changes and anonymous evidence."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request

from test_integration import Account, BODY, Handler, MODEL, Stream, chunk, turn, wb
from test_loop_recovery import LOOP
from wb_ops import FailedReasoningCache


class PhaseTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100.]
        self.enterContext(mock.patch.object(wb.time, 'monotonic', side_effect=lambda: self.clock[0]))
        self.enterContext(mock.patch.dict(os.environ, {'WB_OPS_GENERATION_TIMEOUT':'300', 'WB_OPS_OBSERVE':'0'}))

    def opened(self, header=120, spent=60, total='300'):
        seen = {}
        source = Stream(turn({'content':'answer'}))
        def open_fake(body, **kwargs):
            seen['header_end'] = kwargs['deadline_at']
            seen['prebuilt'] = kwargs.get('prebuilt_body')
            self.clock[0] += spent
            return source, Account(), 'high'
        def probe_fake(raw, **kwargs):
            seen['first_end'] = kwargs['deadline_at']
            return raw, None
        with mock.patch.dict(os.environ, {'WB_OPS_GENERATION_TIMEOUT':total}), \
             mock.patch.object(wb, 'upstream_timeouts', return_value=(header,300)), \
             mock.patch.object(wb, 'open_upstream', side_effect=open_fake), \
             mock.patch.object(wb, 'probe_first_frame', side_effect=probe_fake):
            response, _, _ = wb._open_first_stream(BODY, realm='intl', prebuilt_body=BODY)
        guard = wb._GuardedStream(response, {'model':MODEL,'base_body':BODY})
        try:seen['generation_end'] = guard.deadline
        finally:guard.close()
        return seen

    def test_header_120_is_not_shortened_to_45_and_total_is_not_restarted(self):
        seen = self.opened()
        self.assertEqual(seen['header_end'],220)
        self.assertEqual(seen['first_end'],205)
        self.assertEqual(seen['generation_end'],400)
        self.assertIs(seen['prebuilt'], BODY)

    def test_shorter_header_setting_still_applies(self):
        seen = self.opened(header=10,spent=8)
        self.assertEqual(seen['header_end'],110)
        self.assertEqual(seen['first_end'],153)

    def test_remaining_total_caps_both_phases(self):
        seen = self.opened(spent=49,total='50')
        self.assertEqual((seen['header_end'],seen['first_end'],seen['generation_end']),(150,150,150))

    def test_disabled_total_preserves_independent_phase_limits(self):
        seen = self.opened(total='0')
        self.assertEqual((seen['header_end'],seen['first_end']),(220,205))
        self.assertIsNone(seen['generation_end'])

    def test_probe_replacement_keeps_the_original_total_deadline(self):
        initial,replacement=Stream([]),Stream(turn({'content':'answer'}))
        with mock.patch.object(wb,'open_upstream',return_value=(initial,Account(),None)), \
             mock.patch.object(wb,'probe_first_frame',return_value=(replacement,Account('b'))):
            response,account,_=wb._open_first_stream(BODY)
        self.assertIs(response,replacement)
        self.assertEqual(response._ops_generation_deadline,400)
        self.assertEqual(account.uid,'b')
        response.close()

    def test_recovery_opens_headers_then_gets_a_new_first_frame_phase(self):
        holder={'model':MODEL,'base_body':BODY,'account':Account(),'generation_deadline':400}
        seen={}
        def callback(excluded,deadline_at=None,**kwargs):
            seen['header_end']=deadline_at
            self.clock[0]+=60
            return Stream(turn({'content':'answer'})),Account('b')
        callback._ops_deadline=True
        holder['retry_call']=callback
        with mock.patch.object(wb,'upstream_timeouts',return_value=(120,300)),mock.patch.object(wb,'retry_bump'), \
             mock.patch.object(wb,'probe_first_frame',side_effect=lambda raw,**kw:(seen.update(first_end=kw['deadline_at']) or raw,None)):
            self.assertTrue(wb._start_empty_continuation(holder))
        self.assertEqual((seen['header_end'],seen['first_end']),(220,205))
        self.assertEqual(holder['generation_deadline'],400)
        wb._close_upstream(holder['continue_upstream'])

    def test_expired_total_does_not_open_recovery(self):
        callback=mock.Mock()
        holder={'model':MODEL,'base_body':BODY,'retry_call':callback,'generation_deadline':99}
        with mock.patch.object(wb,'retry_bump'):
            self.assertFalse(wb._start_empty_continuation(holder))
        callback.assert_not_called()
        self.assertEqual(holder['continuation_failure_code'],'generation_timeout')


class LoopbackPhaseTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'WB_OPS_GENERATION_TIMEOUT':'3'}))
        self.enterContext(mock.patch.object(wb,'upstream_timeouts',return_value=(1.,1.)))
        self.enterContext(mock.patch.object(wb,'FIRST_FRAME_DEADLINE',.08))
        self.enterContext(mock.patch.object(wb,'retry_bump'))
        outer=self
        class ServerHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/headers':time.sleep(.18)
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                if self.path=='/body':time.sleep(.25)
                try:self.wfile.write(b''.join(turn({'content':'answer'})))
                except OSError:pass
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),ServerHandler)
        self.server.daemon_threads=True
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def open(self,path,body,**kwargs):
        request=urllib.request.Request('http://127.0.0.1:%d%s'%(self.server.server_port,path))
        response=urllib.request.urlopen(request,timeout=max(.001,kwargs['deadline_at']-time.monotonic()))
        return response,Account(),None

    def test_slow_headers_can_exceed_first_frame_limit(self):
        with mock.patch.object(wb,'open_upstream',side_effect=lambda body,**kw:self.open('/headers',body,**kw)) as opened:
            response,_,_=wb._open_first_stream(BODY)
            try:self.assertIn(b'answer',b''.join(response))
            finally:wb._close_upstream(response)
        self.assertEqual(opened.call_count,1)

    def test_silent_body_still_has_a_first_frame_limit_without_hidden_retry(self):
        with mock.patch.object(wb,'open_upstream',side_effect=lambda body,**kw:self.open('/body',body,**kw)) as opened:
            with self.assertRaises((RuntimeError,TimeoutError,ValueError)):
                wb._open_first_stream(BODY)
        self.assertEqual(opened.call_count,1)


class PrefixTests(unittest.TestCase):
    A='Known failed reasoning one.\n'*10
    B='Known failed reasoning two.\n'*10

    def test_consecutive_prefixes_removed_and_unknown_suffix_exact(self):
        cache=FailedReasoningCache();cache.remember(self.A);cache.remember(self.B)
        self.assertEqual(cache.clean(self.A+self.B+'Useful final reasoning.'),'Useful final reasoning.')
        self.assertEqual(cache.clean('Unknown prefix.'+self.A),'Unknown prefix.'+self.A)

    def test_longest_match_unicode_and_expiry(self):
        clock=[0.];cache=FailedReasoningCache(ttl=5,clock=lambda:clock[0])
        text='已知失败推理。'*40
        cache.remember(text);cache.remember(text+self.A)
        self.assertEqual(cache.clean(text+self.A+'保留结论'),'保留结论')
        clock[0]=6
        self.assertEqual(cache.clean(text+self.A),text+self.A)

    def test_segment_and_character_budgets_bound_cleanup(self):
        cache=FailedReasoningCache();cache.remember(self.A)
        text=self.A*(cache.MAX_CLEAN_SEGMENTS+1)+'Useful.'
        self.assertEqual(cache.clean(text),self.A+'Useful.')
        long='Known failed reasoning.\n'*2000
        cache.remember(long)
        self.assertEqual(cache.clean(long+long+'Useful.'),long+'Useful.')

    def test_backfill_cleans_both_aliases_without_mutating_input(self):
        cache=FailedReasoningCache();cache.remember(self.A);cache.remember(self.B)
        messages=[{'role':'assistant','content':'result','reasoning_content':self.A+self.B+'Useful.',
                   'reasoning':self.A+self.B+'Useful.'}]
        before=copy.deepcopy(messages)
        with mock.patch.object(wb,'_FAILED_REASONING',cache):
            cleaned=wb.backfill_reasoning_content(messages,MODEL,True)
        self.assertEqual(messages,before)
        self.assertEqual(cleaned[0]['reasoning_content'],'Useful.')
        self.assertEqual(cleaned[0]['reasoning'],'Useful.')


class RecoveryDefaultsTests(unittest.TestCase):
    def test_default_hint_is_one_detached_attempt_in_all_six_paths(self):
        original=os.environ.pop('WB_OPS_LOOP_RECOVERY_HINT',None)
        self.addCleanup(lambda:os.environ.__setitem__('WB_OPS_LOOP_RECOVERY_HINT',original) if original is not None else os.environ.pop('WB_OPS_LOOP_RECOVERY_HINT',None))
        for protocol in ('chat','responses','messages'):
            for stream in (True,False):
                with self.subTest(protocol=protocol,stream=stream):
                    body=copy.deepcopy(BODY);before=copy.deepcopy(body)
                    handler=Handler('/v1/'+protocol)
                    args=[Stream([chunk({'reasoning_content':LOOP})]),MODEL]
                    if protocol=='responses':args += [set(),{}]
                    args += [{},Account(),time.time()]
                    with mock.patch.dict(os.environ,{'WB_OPS_LOOP_RETRY':'1','WB_OPS_OBSERVE':'0'}), \
                         mock.patch.object(wb,'record_usage'),mock.patch.object(wb,'record_error'), \
                         mock.patch.object(wb,'apply_daily_token_limit'),mock.patch.object(wb,'retry_bump'), \
                         mock.patch.object(wb,'open_upstream',return_value=(Stream(turn({'content':'Recovered answer.'})),Account('b'),None)) as opened:
                        getattr(handler,'_'+protocol+('_stream_response' if stream else '_nonstream_response'))(*args,base_body=body)
                    self.assertEqual(opened.call_count,1)
                    actual=opened.call_args.args[0]
                    self.assertEqual(actual['messages'][:-1],before['messages'])
                    self.assertEqual(actual['messages'][-1]['role'],'user')
                    self.assertEqual(body,before)
                    self.assertNotIn('reasoning_loop_detected',json.dumps(handler.json_body) if not stream else b''.join(handler.written).decode())


class GuardEvidenceTests(unittest.TestCase):
    def test_rule_metrics_and_retry_stop_reason_survive_restart_without_text(self):
        O=wb.wb_observe
        observation=O.RequestObservation(MODEL,'chat',True,'test','0'*64)
        token=O.CURRENT.set(observation)
        try:
            with mock.patch.dict(os.environ,{'WB_OPS_LOOP_RETRY':'1'}),mock.patch.object(wb,'retry_bump'):
                list(wb._stream_chat_events(Stream([chunk({'reasoning_content':LOOP})]),MODEL,{'base_body':BODY}))
            observation.finish_attempt('upstream_aborted')
        finally:O.CURRENT.reset(token)
        with tempfile.TemporaryDirectory(dir=os.environ['WB_OPS_TEST_STATE']) as directory:
            store=O.ObservationStore(Path(directory)/'observations.jsonl')
            with mock.patch.dict(os.environ,{'WB_OPS_OBSERVE':'1'}):store.append(observation.records())
            snapshot=O.ObservationStore(store.path).snapshot()
        request=snapshot['recent'][-1]
        self.assertEqual(request['reason'],'reasoning_loop_detected')
        detail=request['guard_details'][0]
        self.assertIn(detail['rule'],O.GUARD_RULES)
        self.assertGreater(detail['received_chars'],0)
        self.assertGreater(detail['window_lines'],0)
        self.assertEqual(detail['content_seen'],0)
        self.assertEqual(request['signals']['retry_budget_exhausted'],1)
        self.assertNotIn('Let me',json.dumps(snapshot))

    def test_untrusted_detail_values_are_filtered_and_bounded(self):
        O=wb.wb_observe
        detail=O.clean_guard_detail({'kind':'reasoning_loop_detected','rule':'PRIVATE_RULE',
                    'prompt':'PRIVATE_TEXT','window_chars':10**50,'intent_lines':'PRIVATE_TEXT'})
        self.assertEqual(detail['rule'],'unknown')
        self.assertEqual(detail['window_chars'],9007199254740991)
        self.assertNotIn('PRIVATE_',json.dumps(detail))


class ReferenceHardeningTests(unittest.TestCase):
    def test_short_quoted_names_do_not_exempt_repeated_surrounding_plan(self):
        for left,right in [('"','"'),('“','”'),("'","'"),('‘','’')]:
            text=('Let me write the actual tool call '+left+'lookup'+right+'.\n')*100
            for size in (1,17,64,len(text)):
                guard=wb.ReasoningLoopGuard()
                for start in range(0,len(text),size):
                    guard.feed(text[start:start+size])
                self.assertTrue(guard.hit,(left,size))

    def test_quoted_loop_on_same_line_preserves_surrounding_answer(self):
        text='Diagnostic sample: "'+('Let me write. '*180)+'" The answer is complete.\n'
        for size in (1,17,64,len(text)):
            guard=wb.ReasoningLoopGuard()
            for start in range(0,len(text),size):
                guard.feed(text[start:start+size])
            self.assertFalse(guard.hit)

    def test_usage_rejects_malformed_counts_and_nonfinite_values(self):
        good={'prompt_tokens':71,'completion_tokens':22,'total_tokens':93}
        for bad in ({},{'other':1},{'total_tokens':True},{'total_tokens':-1},
                    {'total_tokens':float('nan')},{'total_tokens':float('inf')},
                    {'prompt_tokens':'100'}):
            self.assertIs(wb._prefer_stream_usage(good,bad),good)

    def test_cumulative_usage_without_total_keeps_larger_snapshot(self):
        good={'prompt_tokens':71,'completion_tokens':22,
              'completion_tokens_details':{'reasoning_tokens':11}}
        saved=wb._prefer_stream_usage(None,good)
        good['completion_tokens_details']['reasoning_tokens']=999
        self.assertEqual(saved['completion_tokens_details']['reasoning_tokens'],11)
        self.assertIs(wb._prefer_stream_usage(saved,{'prompt_tokens':1,'completion_tokens':1}),saved)
        newer=wb._prefer_stream_usage(saved,{'prompt_tokens':71,'completion_tokens':30})
        self.assertEqual(newer['completion_tokens'],30)


if __name__=='__main__':unittest.main()

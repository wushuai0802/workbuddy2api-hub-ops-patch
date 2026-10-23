"""v1.7.8 cache/identity/governance interactions; no model calls."""
import copy
import json
import os
import socket
import time
import unittest
from unittest import mock

from test_integration import Account, BODY, Handler, MODEL, Stream, chunk, responses, turn, wb
from test_guarded_sse_batching import ChunkSource


class V178BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ,{'WB_OPS_LOOP_RETRY':'0','WB_OPS_OBSERVE':'0'}))
        self.enterContext(mock.patch.object(wb,'record_usage'))
        self.enterContext(mock.patch.object(wb,'record_error'))
        self.enterContext(mock.patch.object(wb,'apply_daily_token_limit'))

    def invoke(self,protocol,stream,usage):
        handler=Handler('/v1/'+protocol)
        source=ChunkSource([chunk({'content':'Synthetic result.'})+chunk(finish='stop',usage=usage)+b'data: [DONE]\n\n'])
        args=[source,MODEL]
        if protocol=='responses':args += [set(),{}]
        args += [{},Account(),time.time()]
        with mock.patch.object(wb,'open_upstream',side_effect=AssertionError('unexpected model request')):
            getattr(handler,'_'+protocol+('_stream_response' if stream else '_nonstream_response'))(*args,base_body=BODY)
        self.assertTrue(source.closed)
        return handler

    def test_cache_hit_is_exposed_in_protocol_canonical_fields_all_six_paths(self):
        for protocol in ('chat','responses','messages'):
            for stream in (True,False):
                for cache in ({'cached_tokens':17},{'input_tokens_details':{'cached_tokens':17}},
                              {'cache_read_input_tokens':0,'prompt_tokens_details':{'cached_tokens':17}}):
                    usage=dict(prompt_tokens=71,completion_tokens=22,total_tokens=93,**cache)
                    before=copy.deepcopy(usage)
                    with self.subTest(protocol=protocol,stream=stream,cache=cache):
                        h=self.invoke(protocol,stream,usage)
                        if protocol=='chat':
                            result=next(frame['usage'] for frame in responses(h) if frame.get('usage')) if stream else h.json_body['usage']
                            self.assertEqual(result['prompt_tokens_details']['cached_tokens'],17)
                        elif protocol=='responses':
                            result=next(frame['response']['usage'] for frame in responses(h) if frame.get('type')=='response.completed') if stream else h.json_body['usage']
                            self.assertEqual(result['input_tokens_details']['cached_tokens'],17)
                        else:
                            all_usage=[frame.get('usage') or frame.get('message',{}).get('usage') for frame in responses(h)] if stream else [h.json_body['usage']]
                            self.assertTrue(any(u and u.get('cache_read_input_tokens')==17 for u in all_usage))
                    self.assertEqual(usage,before)

    def test_aggregate_usage_is_detached_from_received_snapshot(self):
        usage={'prompt_tokens':10,'completion_tokens':1,'total_tokens':11,'cached_tokens':7}
        holder={}
        result=wb.aggregate_stream(Stream(turn({'content':'answer'},tokens=11)+[chunk(usage=usage)]),MODEL,None,holder)
        result['usage']['cached_tokens']=999
        self.assertEqual(holder['ops_received_usage']['cached_tokens'],7)

    def test_invalid_cache_aliases_do_not_hide_valid_hits_six_paths(self):
        variants = [
            {'prompt_tokens_details': 'invalid', 'input_tokens_details': ['invalid'], 'cached_tokens': 17},
            {'prompt_cache_hit_tokens': True, 'cache_read_input_tokens': float('inf'), 'cached_tokens': 17},
        ]
        for protocol in ('chat', 'responses', 'messages'):
            for stream in (True, False):
                for cache in variants:
                    with self.subTest(protocol=protocol, stream=stream, cache=cache):
                        usage = dict(prompt_tokens=71, completion_tokens=22, total_tokens=93, **cache)
                        original = copy.deepcopy(usage)
                        handler = self.invoke(protocol, stream, usage)
                        if protocol == 'chat':
                            value = next(row['usage'] for row in responses(handler) if row.get('usage')) if stream else handler.json_body['usage']
                            self.assertEqual(value['prompt_tokens_details']['cached_tokens'], 17)
                        elif protocol == 'responses':
                            value = next(row['response']['usage'] for row in responses(handler) if row.get('type') == 'response.completed') if stream else handler.json_body['usage']
                            self.assertEqual(value['input_tokens_details']['cached_tokens'], 17)
                        else:
                            values = [row.get('usage') or row.get('message', {}).get('usage') for row in responses(handler)] if stream else [handler.json_body['usage']]
                            self.assertTrue(any(value and value.get('cache_read_input_tokens') == 17 for value in values))
                        self.assertEqual(usage, original)

    def test_invalid_detail_shapes_and_nonfinite_cache_values_are_ignored(self):
        for details in (None, 'invalid', ['invalid'], 3, True):
            for bad in (True, -1, float('inf'), float('-inf'), float('nan'), 10 ** 400):
                usage = {'prompt_tokens_details': details, 'input_tokens_details': details,
                         'completion_tokens_details': details, 'prompt_cache_hit_tokens': bad, 'cached_tokens': 17}
                with self.subTest(details=type(details).__name__, bad=type(bad).__name__):
                    self.assertEqual(wb._best_cached_tokens(usage), 17)

    def test_output_normalization_preserves_original_provider_ledger_six_paths(self):
        usage={'prompt_tokens':71,'completion_tokens':22,'total_tokens':93,'cached_tokens':17}
        for protocol in ('chat','responses','messages'):
            for stream in (True,False):
                with self.subTest(protocol=protocol,stream=stream),mock.patch.object(wb,'record_usage') as recorded:
                    self.invoke(protocol,stream,usage)
                    recorded.assert_called_once()
                    self.assertEqual(recorded.call_args.args[1],usage)

    def test_local_network_wait_respects_original_header_deadline_without_penalty(self):
        account=wb.wb_accounts.Account({'uid':'synthetic-deadline','realm':'intl','accessToken':'synthetic'})
        pool=mock.Mock();pool.accounts=[account];pool.count_ready.return_value=1;pool.pick_for_session.return_value=account
        clock=[100.]
        def wait(seconds):clock[0]+=seconds
        with mock.patch.object(wb,'POOL',pool),mock.patch.object(wb.time,'monotonic',side_effect=lambda:clock[0]), \
             mock.patch.object(wb.time,'sleep',side_effect=wait),mock.patch.object(wb.wb_accounts,'urlopen',side_effect=socket.gaierror(-3,'Temporary failure in name resolution')) as opened:
            with self.assertRaises(TimeoutError):wb.open_upstream(BODY,target_realm='intl',deadline_at=100.05)
        self.assertAlmostEqual(clock[0],100.05)
        self.assertEqual(account.fails,0)
        self.assertEqual(opened.call_count,1)


if __name__=='__main__':unittest.main()

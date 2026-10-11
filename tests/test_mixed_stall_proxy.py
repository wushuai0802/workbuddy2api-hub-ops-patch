"""Offline protection/compatibility checks against the current v1.6.19 gateway."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolated_dirs import isolated_data_dirs

_TMP = isolated_data_dirs('wb-mixed-stall-proxy-')
os.environ['WB_OPS_OFFLINE'] = '1'
import wb_proxy as P

MODEL = 'deepseek-v4.1-flash'
BODY = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'fixture'}], 'stream': True}
MIXED = ('Hmm.\nHmm.\nLet me go.\n'
         'The candidate needs another comparison before a concrete result.\n') * 80


def chunk(delta=None, finish=None, usage=None):
    frame = {'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]}
    if usage:
        frame['usage'] = usage
    return ('data: ' + json.dumps(frame) + '\n\n').encode()


class Stream:
    def __init__(self, frames):
        self.frames, self.closed = frames, False

    def __iter__(self):
        yield from self.frames

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Account:
    uid = 'fixture-account'
    realm = 'intl'


def stalled_frames():
    return [chunk({'reasoning_content': MIXED[i:i+47]}) for i in range(0, len(MIXED), 47)] + [
        chunk(finish='stop'), b'data: [DONE]\n\n']


def good_frames():
    return [chunk({'content': 'OK'}), chunk(finish='stop', usage={
        'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}), b'data: [DONE]\n\n']


def decode(frames):
    result = []
    for frame in frames:
        for line in frame.decode().splitlines():
            if line.startswith('data: ') and line != 'data: [DONE]':
                result.append(json.loads(line[6:]))
    return result


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'WB_OPS_LOOP_GUARD': '1',
                           'WB_OPS_MIXED_STALL_GUARD': '1', 'WB_OPS_MIXED_STALL_HISTORY': '1',
                           'WB_OPS_LOOP_RETRY': '1', 'WB_OPS_GENERATION_TIMEOUT': '30'}))
        self.enterContext(mock.patch.object(P, 'POOL', None))

    def test_new_rule_covers_a_mixed_fixture_the_old_guard_misses(self):
        guard = P.ReasoningLoopGuard()
        self.assertFalse(guard.feed(MIXED))
        holder = {'model': MODEL, 'base_body': BODY}
        frames = decode(list(P._GuardedStream(Stream(stalled_frames()), holder)))
        self.assertTrue(any(item.get('error', {}).get('code') == 'reasoning_loop_detected' for item in frames))
        self.assertEqual(holder['loop_evidence']['rule'], 'mixed-stall')

    def test_other_models_and_guard_opt_out_remain_unchanged(self):
        frames = stalled_frames()
        self.assertEqual(list(P._GuardedStream(Stream(frames), {'model': 'other-model'})), frames)
        self.assertEqual(list(P._GuardedStream(Stream(frames), {'model': MODEL, 'base_body': {'n': 2}})), frames)
        with mock.patch.dict(os.environ, {'WB_OPS_LOOP_GUARD': '0'}):
            self.assertEqual(list(P._GuardedStream(Stream(frames), {'model': MODEL})), frames)
        with mock.patch.dict(os.environ, {'WB_OPS_MIXED_STALL_GUARD': '0'}):
            self.assertEqual(list(P._GuardedStream(Stream(frames), {'model': MODEL})), frames)

    def test_visible_output_and_tools_block_replay(self):
        for first in ({'content': 'A concrete result.'}, {'tool_calls': [{
                'index': 0, 'id': 'fixture-call', 'function': {'name': 'lookup', 'arguments': '{}'}}]}):
            with self.subTest(first=first):
                retry = mock.Mock()
                holder = {'model': MODEL, 'base_body': BODY, 'retry_call': retry, 'account': Account()}
                result, _ = P._aggregate_with_recovery(
                    Stream([chunk(first)] + stalled_frames()), MODEL, Account(), holder)
                self.assertEqual(result['_ops_failure'], 'reasoning_loop_detected')
                retry.assert_not_called()

    def test_one_safe_recovery_then_success(self):
        retry = mock.Mock(return_value=(Stream(good_frames()), Account()))
        holder = {'model': MODEL, 'base_body': BODY, 'retry_call': retry, 'account': Account()}
        result, _ = P._aggregate_with_recovery(Stream(stalled_frames()), MODEL, Account(), holder)
        self.assertEqual(result['choices'][0]['message']['content'], 'OK')
        self.assertNotIn('_ops_failure', result)
        retry.assert_called_once()

    def test_recovery_budget_stops_a_second_stall(self):
        retry = mock.Mock(return_value=(Stream(stalled_frames()), Account()))
        holder = {'model': MODEL, 'base_body': BODY, 'retry_call': retry, 'account': Account()}
        result, _ = P._aggregate_with_recovery(Stream(stalled_frames()), MODEL, Account(), holder)
        self.assertEqual(result['_ops_failure'], 'reasoning_loop_detected')
        retry.assert_called_once()

    def test_three_stream_protocols_report_failure_without_fake_completion(self):
        with mock.patch.dict(os.environ, {'WB_OPS_LOOP_RETRY': '0'}):
            for protocol in ('chat', 'responses', 'messages'):
                with self.subTest(protocol=protocol):
                    holder = {'model': MODEL, 'base_body': BODY, 'account': Account()}
                    source = Stream(stalled_frames())
                    if protocol == 'chat':
                        frames = list(P._stream_chat_events(source, MODEL, holder))
                    elif protocol == 'responses':
                        frames = list(P.stream_responses_events(source, MODEL, holder))
                    else:
                        frames = list(P.stream_messages_events(P._stream_chat_events(source, MODEL, holder), MODEL))
                    values = decode(frames)
                    self.assertIn('reasoning_loop_detected', json.dumps(values))
                    self.assertFalse(any(value.get('type') == 'response.completed' for value in values))

    def test_outgoing_cleanup_preserves_content_tools_results_and_input(self):
        call = {'id': 'fixture-call', 'type': 'function',
                'function': {'name': 'lookup', 'arguments': '{"value":1}'}}
        messages = [{'role': 'user', 'content': 'Hmm. Let me go.'},
                    {'role': 'assistant', 'content': 'Concrete result.', 'tool_calls': [call],
                     'reasoning_content': MIXED, 'reasoning': MIXED},
                    {'role': 'tool', 'tool_call_id': 'fixture-call', 'content': 'business-output'}]
        before = copy.deepcopy(messages)
        output = P.clean_deepseek_stalled_history(messages, MODEL)
        self.assertEqual(messages, before)
        self.assertEqual(output[0], before[0])
        self.assertEqual(output[2], before[2])
        for field in ('content', 'tool_calls'):
            self.assertEqual(output[1][field], before[1][field])
        for field in ('reasoning_content', 'reasoning'):
            self.assertLess(len(output[1][field]), len(MIXED))
            self.assertEqual(output[1][field].count('The candidate needs another comparison'), 80)
        body = P.build_upstream_body({'model': MODEL, 'messages': messages})
        self.assertEqual(messages, before)
        assistant = next(message for message in body['messages'] if message.get('tool_calls'))
        self.assertEqual(assistant['content'], before[1]['content'])
        self.assertLess(len(assistant['reasoning']), len(MIXED))

    def test_cleanup_scope_and_switch(self):
        messages = [{'role': 'assistant', 'reasoning_content': MIXED}]
        self.assertIs(P.clean_deepseek_stalled_history(messages, 'other-model'), messages)
        with mock.patch.dict(os.environ, {'WB_OPS_MIXED_STALL_HISTORY': '0'}):
            self.assertIs(P.clean_deepseek_stalled_history(messages, MODEL), messages)

    def test_real_progress_short_prefaces_preserve_frames_and_usage(self):
        call = {'tool_calls': [{'index': 0, 'id': 'fixture-call', 'function': {
            'name': 'lookup', 'arguments': '{"id":2}'}}]}
        usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
        frames = [chunk({'reasoning_content': 'Hmm. Let me go. The checksum changed.'}),
                  chunk(call), chunk(finish='tool_calls', usage=usage), b'data: [DONE]\n\n']
        self.assertEqual(list(P._GuardedStream(Stream(frames), {'model': MODEL, 'base_body': BODY})), frames)


if __name__ == '__main__':
    unittest.main()

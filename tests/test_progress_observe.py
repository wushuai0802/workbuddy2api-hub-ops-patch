"""History signals and durable request/attempt metrics must remain observational."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from test_integration import Account, BODY, Handler, MODEL, Stream, chunk, turn, wb
from test_guarded_sse_batching import ChunkSource
from test_review_fixes import REPEAT, USAGE

O = wb.wb_observe


def history(count=12, result='Error: prerequisite missing.', tool='lookup'):
    messages = [{'role': 'user', 'content': 'synthetic original task'}]
    for i in range(count):
        messages += [{'role': 'assistant', 'tool_calls': [{'id': 'c'+str(i), 'type': 'function',
                      'function': {'name': tool, 'arguments': json.dumps({'attempt': i})}}]},
                     {'role': 'tool', 'tool_call_id': 'c'+str(i),
                      'content': result(i) if callable(result) else result}]
    return messages


class ProgressTests(unittest.TestCase):
    def test_changed_arguments_repeated_failure_is_observed_without_rewrite(self):
        messages = history()
        before = copy.deepcopy(messages)
        signal = O.progress_signal(messages, MODEL)
        self.assertEqual(signal, {'kind': 'progress_repeated_failure', 'paired_rounds': 12,
                                 'repeated_results': 12, 'distinct_arguments': 12})
        self.assertEqual(messages, before)
        self.assertNotIn('prerequisite', json.dumps(signal))

    def test_one_hundred_rounds_with_new_evidence_remain_unflagged(self):
        self.assertIsNone(O.progress_signal(history(100, lambda i: json.dumps({'checksum': i, 'exit_code': 0})), MODEL))

    def test_one_hundred_polling_rounds_remain_unflagged(self):
        for name, result in [('write_stdin', 'same status'), ('poll', 'same status'),
                             ('lookup', 'Process running with session ID 42'),
                             ('lookup', json.dumps({'status': 'pending', 'session_id': 'synthetic'}))]:
            self.assertIsNone(O.progress_signal(history(100, result, name), MODEL))

    def test_new_user_task_and_unpaired_calls_reset_history_scope(self):
        messages = history() + [{'role': 'user', 'content': 'new independent task'}]
        self.assertIsNone(O.progress_signal(messages, MODEL))
        messages = history()
        for message in messages:
            if message['role'] == 'tool':
                message['tool_call_id'] = 'not-paired'
        self.assertIsNone(O.progress_signal(messages, MODEL))

    def test_different_models_partial_and_large_results_are_not_inferred(self):
        self.assertIsNone(O.progress_signal(history(), 'another-model'))
        self.assertIsNone(O.progress_signal(history(7), MODEL))
        self.assertIsNone(O.progress_signal(history(12, 'Error: '+('x'*70000)), MODEL))

    def test_multiple_calls_are_paired_by_id_not_message_order(self):
        messages = history()
        outputs = [m for m in messages if m['role'] == 'tool']
        reordered = [messages[0]] + [m for m in messages[1:] if m['role'] == 'assistant'] + list(reversed(outputs))
        self.assertEqual(O.progress_signal(reordered, MODEL)['repeated_results'], 12)

    def test_malformed_extensions_are_ignored_without_crashing(self):
        for messages in [[None, 3, {}], [{'role': 'assistant', 'tool_calls': 'invalid'}],
                         [{'role': 'tool', 'tool_call_id': [], 'content': 'Error: bad'}]]:
            self.assertIsNone(O.progress_signal(messages, MODEL))

    def test_parallel_independent_histories_do_not_share_state(self):
        outputs = []
        def run(messages):
            outputs.append(O.progress_signal(messages, MODEL))
        threads = [threading.Thread(target=run, args=(m,)) for m in (history(), history(100, lambda i: str(i)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(v is None for v in outputs), 1)


class DurableObservationTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ['WB_OPS_TEST_STATE'])))
        self.store = O.ObservationStore(root/'ops-observations.jsonl')
        self.enterContext(mock.patch.dict(os.environ, {'WB_OPS_OBSERVE': '1', 'WB_OPS_PROGRESS_OBSERVE': '1',
                          'WB_OPS_LOOP_RETRY': '0', 'WB_OPS_GENERATION_TIMEOUT': '30'}))
        self.enterContext(mock.patch.object(wb, '_observation_store', return_value=self.store))
        self.enterContext(mock.patch.object(wb, '_persist_usage'))
        self.enterContext(mock.patch.object(wb, 'log'))
        self.enterContext(mock.patch.object(wb, 'retry_bump'))
        self.enterContext(mock.patch.object(wb, 'apply_daily_token_limit'))
        self.enterContext(mock.patch.object(wb, 'POOL', None))

    def invoke(self, protocol, stream, source, body=BODY, writer_error=None):
        handler = Handler('/v1/'+{'chat':'chat/completions', 'responses':'responses', 'messages':'messages'}[protocol], writer_error=writer_error)
        args = [source, MODEL]
        if protocol == 'responses':
            args += [set(), {}]
        args += [{}, Account(), wb.time.time()]
        getattr(handler, '_'+protocol+('_stream_response' if stream else '_nonstream_response'))(
            *args, base_body=body, session_key='PRIVATE_SESSION_DO_NOT_SAVE')
        return handler

    def test_six_paths_record_one_request_and_one_attempt(self):
        for protocol in ('chat', 'responses', 'messages'):
            for stream in (True, False):
                with self.subTest(protocol=protocol, stream=stream):
                    before = len(self.store.snapshot()['recent'])
                    source = ChunkSource([chunk({'content': 'PRIVATE_RESULT'})+chunk(finish='stop', usage=USAGE)+b'data: [DONE]\n\n'])
                    self.invoke(protocol, stream, source)
                    rows = self.store.snapshot()['recent'][before:]
                    self.assertEqual([r['phase'] for r in rows], ['attempt', 'request'])
                    self.assertTrue(all(r['outcome']=='completed' for r in rows))
                    self.assertTrue(all(r['usage']['total_tokens']==93 for r in rows))
                    self.assertTrue(all(r['usage_source']=='provider' for r in rows))
                    self.assertTrue(all(r['session_present'] for r in rows))
                    self.assertNotIn('PRIVATE_', json.dumps(rows))
                    self.assertIsNone(O.CURRENT.get())

    def test_safe_recovery_is_two_attempts_one_request_not_business_completion(self):
        first = Stream([chunk({'reasoning_content': REPEAT}), chunk(finish='stop', usage=USAGE)])
        with mock.patch.dict(os.environ, {'WB_OPS_LOOP_RETRY':'1'}), mock.patch.object(
                wb, 'open_upstream', return_value=(Stream(turn({'content':'valid result'})), Account('b'))):
            self.invoke('chat', True, first)
        snap = self.store.snapshot()
        attempts = [r for r in snap['recent'] if r['phase']=='attempt']
        requests = [r for r in snap['recent'] if r['phase']=='request']
        self.assertEqual(len(attempts), 2)
        self.assertEqual([r['outcome'] for r in attempts], ['failed', 'completed'])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['attempts'], 2)
        self.assertEqual(requests[0]['recovery_with_output'], 1)
        self.assertEqual(snap['business_completion'], 'unmeasured')

    def test_disconnect_and_guard_failure_do_not_count_as_recovery(self):
        for protocol in ('chat', 'responses', 'messages'):
            for case in ('disconnect', 'loop'):
                with self.subTest(protocol=protocol, case=case):
                    field = 'content'
                    source = ChunkSource([chunk({field: REPEAT if case=='loop' else 'answer'})+
                                          chunk(finish='stop', usage=USAGE)+b'data: [DONE]\n\n'])
                    self.invoke(protocol, True, source, writer_error=BrokenPipeError() if case=='disconnect' else None)
                    request = self.store.snapshot()['recent'][-1]
                    self.assertEqual(request['phase'], 'request')
                    expected = 'client_aborted' if case=='disconnect' else ('failed' if protocol=='messages' else 'upstream_aborted')
                    self.assertEqual(request['outcome'], expected)
                    self.assertEqual(request['recovery_with_output'], 0)
                    self.assertEqual(request['usage']['total_tokens'], 93)

    def test_progress_observation_changes_neither_wire_nor_retry_budget(self):
        body = dict(BODY, messages=history())
        wire = turn({'content':'normal response'})
        with mock.patch.object(wb, 'open_upstream') as opened:
            handler = self.invoke('chat', True, Stream(wire), body)
        opened.assert_not_called()
        self.assertIn(b'normal response', b''.join(handler.written))
        request = self.store.snapshot()['recent'][-1]
        self.assertEqual(request['signals']['progress_repeated_failure'], 1)
        self.assertEqual(request['paired_rounds'], 12)
        self.assertEqual(request['outcome'], 'completed')

    def test_restart_and_since_until_model_filters(self):
        self.invoke('chat', True, Stream(turn({'content':'result'})))
        rows = self.store.snapshot()['recent']
        recreated = O.ObservationStore(self.store.path)
        self.assertEqual(recreated.snapshot()['recent'], rows)
        at = rows[-1]['at']
        self.assertEqual(len(recreated.snapshot(since=at, until=at, model=MODEL)['recent']), 2)
        self.assertEqual(recreated.snapshot(since=at+1)['groups'], [])
        self.assertEqual(recreated.snapshot(model='another-model')['groups'], [])

    def test_rotation_concurrency_schema_and_privacy(self):
        store = O.ObservationStore(self.store.path, max_bytes=4096, backups=2)
        record = O.RequestObservation(MODEL, 'chat', True, 'test', '0'*64)
        record.begin({})
        record.finish_attempt('completed', USAGE)
        rows = record.records()
        rows[0].update(prompt='PRIVATE_PROMPT', account='PRIVATE_ACCOUNT', tool='PRIVATE_TOOL')
        rows[0]['signals'].update(PRIVATE_LABEL=1)
        workers = [threading.Thread(target=lambda: [store.append(rows) for _ in range(30)]) for _ in range(4)]
        for t in workers:
            t.start()
        for t in workers:
            t.join()
        snap = store.snapshot()
        self.assertTrue(snap['recent'])
        self.assertNotIn('PRIVATE_', json.dumps(snap))
        for path in Path(store.path).parent.glob('ops-observations.jsonl*'):
            self.assertLessEqual(path.stat().st_size, 4096)
        self.assertLessEqual(len(snap['recent']), 64)

    def test_disabled_metrics_do_not_create_file(self):
        with mock.patch.dict(os.environ, {'WB_OPS_OBSERVE':'0'}):
            self.invoke('chat', True, Stream(turn({'content':'result'})))
        self.assertFalse(Path(self.store.path).exists())

    def test_telemetry_write_failure_does_not_change_response_and_is_reported(self):
        with mock.patch.object(O.os, 'open', side_effect=PermissionError('synthetic write failure')):
            handler = self.invoke('chat', True, Stream(turn({'content':'working response'})))
        self.assertIn(b'working response', b''.join(handler.written))
        self.assertEqual(self.store.snapshot()['write_failures_this_process'], 1)

    def test_invalid_persisted_data_never_exposes_dynamic_values(self):
        Path(self.store.path).write_text('{"schema":1,"phase":"request","prompt":"PRIVATE_PROMPT"}\n', encoding='utf8')
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot['invalid_rows'], 1)
        self.assertEqual(snapshot['groups'], [])
        self.assertNotIn('PRIVATE_', json.dumps(snapshot))


if __name__ == '__main__':
    unittest.main()

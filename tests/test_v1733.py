"""v1.7.33 tool deltas and cancelled recovery retain exact response ownership."""
from contextlib import ExitStack
import http.client
import json
import os
import socket
import threading
import time
import unittest
from unittest import mock

from test_heartbeat_lifecycle import HeartbeatLifecycleTests, LiveHandler
from test_guarded_sse_batching import ChunkSource
from test_integration import Account, BODY, MODEL, chunk, turn, wb


class V1733Tests(HeartbeatLifecycleTests):
    def test_actual_upstream_socket_cancel_releases_account_slot(self):
        for api in ('chat', 'responses', 'messages'):
            with self.subTest(api=api), ExitStack() as stack:
                beats = self.context(stack)
                stack.enter_context(mock.patch.dict(os.environ, {'WB_OPS_GENERATION_TIMEOUT': '.9'}))
                stack.enter_context(mock.patch.dict(wb._ACCOUNT_INFLIGHT, {}, clear=True))
                account = Account('socket-owner')
                client, peer = socket.socketpair()
                stack.callback(client.close)
                stack.callback(peer.close)
                client.settimeout(1.2)
                peer.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n'
                             + chunk({'role': 'assistant'}))
                upstream = http.client.HTTPResponse(client)
                upstream.begin()
                self.assertTrue(wb.account_slot_acquire(account, limit=1))
                wb.bind_account_slot_to_response(upstream, account)
                handler = LiveHandler(api, fail_heartbeat=True)
                errors = []
                def invoke():
                    try:
                        self.invoke(api, handler, upstream)
                    except BaseException as exc:
                        errors.append(exc)
                thread = threading.Thread(target=invoke, daemon=True)
                started = time.monotonic()
                thread.start()
                try:
                    self.assertTrue(handler.beat.wait(1))
                    thread.join(1.5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    self.assertLess(time.monotonic() - started, .7)
                    self.assertTrue(upstream.isclosed())
                    self.assertEqual(wb.account_inflight_snapshot(), {})
                    self.assertNotIn(b'response.completed', b''.join(handler.written))
                    self.assert_stopped(beats)
                finally:
                    peer.close()
                    thread.join(2)
                    if not thread.is_alive():
                        upstream.close()

    def test_cancelled_recovery_never_calls_connector_or_records_attempt(self):
        connector, record = mock.Mock(), mock.Mock()
        holder = {'ops_client_closed': True, 'retry_call': connector, 'record_attempt': record}
        with self.assertRaises(wb._ClientStreamClosed):
            wb._start_empty_continuation(holder)
        connector.assert_not_called()
        record.assert_not_called()

    def test_cancel_during_recovery_closes_new_response_without_probe(self):
        source = ChunkSource([b''.join(turn({'content': 'must not be read'}))])
        holder = {'base_body': BODY}
        def connect(_excluded):
            holder['ops_client_closed'] = True
            return source, Account('late-response')
        holder['retry_call'] = connect
        with mock.patch.object(wb, 'probe_first_frame') as probe:
            with self.assertRaises(wb._ClientStreamClosed):
                wb._start_empty_continuation(holder)
        probe.assert_not_called()
        self.assertTrue(source.closed)
        self.assertEqual(source.reads, 0)
        self.assertNotIn('continue_upstream', holder)

    def test_cancellation_before_first_frame_does_not_read_or_retry(self):
        source = ChunkSource([b''.join(turn({'content': 'unused'}))])
        retry = mock.Mock()
        with self.assertRaises(wb._ClientStreamClosed):
            wb.probe_first_frame(source, retry_call=retry, cancel_holder={'ops_client_closed': True})
        self.assertEqual(source.reads, 0)
        self.assertTrue(source.closed)
        retry.assert_not_called()

    def test_windows_interrupt_keeps_released_pool_socket_owned_by_next_request(self):
        sock = mock.Mock()
        released = mock.Mock(_pool_conn=mock.Mock(sock=sock), _released=True)
        with mock.patch.object(wb, 'IS_WINDOWS', True):
            wb._interrupt_upstream(released)
        sock.shutdown.assert_not_called()
        sock._real_close.assert_not_called()
        self.assertTrue(released._released)

    def test_native_first_tool_deltas_match_final_arguments_once(self):
        for custom in (False, True):
            for size in (1, 17, 65536):
                with self.subTest(custom=custom, size=size):
                    frames = [chunk({'tool_calls': [
                        {'index': i, 'id': 'call-' + str(i), 'function': {'name': 'inspect', 'arguments': '{"n":'}}
                        for i in (0, 1)]}),
                        chunk({'tool_calls': [{'index': i, 'function': {'arguments': str(i) + '}'}} for i in (1, 0)]})]
                    frames += turn({}, 'tool_calls')
                    data = b''.join(frames)
                    source = ChunkSource([data[pos:pos + size] for pos in range(0, len(data), size)])
                    holder = {'base_body': BODY, 'custom_names': {'inspect'} if custom else set()}
                    wire = b''.join(wb.stream_responses_events(source, MODEL, holder))
                    events = [json.loads(line[6:]) for line in wire.splitlines() if line.startswith(b'data: {')]
                    event_type = 'response.custom_tool_call_input.delta' if custom else 'response.function_call_arguments.delta'
                    field = 'input' if custom else 'arguments'
                    for i in (0, 1):
                        call = 'call-' + str(i)
                        deltas = [e['delta'] for e in events if e.get('type') == event_type and e['call_id'] == call]
                        self.assertEqual(deltas, ['{"n":', str(i) + '}'])
                        final = next(e['item'] for e in events if e.get('type') == 'response.output_item.done'
                                     and e['item'].get('call_id') == call)
                        self.assertEqual(final[field], ''.join(deltas))
                    seq = [e['sequence_number'] for e in events]
                    self.assertEqual(seq, list(range(seq[0], seq[0] + len(seq))))


if __name__ == '__main__':
    suite = unittest.TestSuite(V1733Tests(name) for name in V1733Tests.__dict__ if name.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

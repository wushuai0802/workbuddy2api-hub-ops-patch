"""Real HTTP line iteration: SSE separators are not output metadata."""
import http.client
import io
import json
import unittest
from unittest import mock
from test_extra_fields import ExtraFieldsTests, MODES, WIRE
from test_nullable_buffer import frames
from test_metadata_buffer import META, HEART
from test_integration import Account, Stream, chunk, turn, wb
from test_loop_recovery import GOOD_CALL


class FragmentedRaw(io.RawIOBase):
    def __init__(self, data, size):
        self.data, self.size, self.pos = data, size, 0

    def readable(self):
        return True

    def readinto(self, target):
        count=min(len(target),self.size,len(self.data)-self.pos)
        target[:count]=self.data[self.pos:self.pos+count]
        self.pos+=count
        return count


def http_stream(events, size=7):
    """Only the network transport is fake; parser/iteration are HTTPResponse."""
    body=b''.join(events)
    wire=b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body
    raw=FragmentedRaw(wire,size)
    sock=mock.Mock();sock.makefile.return_value=io.BufferedReader(raw)
    response=http.client.HTTPResponse(sock);response.begin()
    return response


class SSEFramingTests(ExtraFieldsTests):
    def test_real_http_iteration_discards_failed_thought_all_apis(self):
        for size in (1,7,4096):
            for api in MODES:
                self.clear_events()
                source=[META,chunk(WIRE)]+frames(WIRE,128)
                with mock.patch.object(wb.time,'monotonic',return_value=0),mock.patch.object(wb,'open_upstream',return_value=(http_stream(turn({'content':'answer'}),size),Account('b'))) as opened:
                    h=self.invoke(api,http_stream(source,size))
                self.assertEqual(opened.call_count,1)
                self.assertIn('answer',self.output(h));self.assertNotIn('Let me run',self.output(h))
                self.assertEqual(self.counts().get('reasoning_buffer_discarded'),1)
                self.assertNotIn('reasoning_buffer_release_metadata',self.counts())
                self.assertEqual(self.counts().get('retry_recovered'),1)

    def test_normal_utf8_answer_and_tool_survive_transport_partitioning(self):
        for size in (1,17,8192):
            for api in MODES:
                for final,finish in (({'content':'answer'},'stop'),(GOOD_CALL,'tool_calls')):
                    data=frames(WIRE,text='中文 evidence. 🚀')+[HEART]+turn(dict(final,extra_fields=None),finish)
                    # Put actual multibyte UTF-8 on the wire, split at any byte.
                    data=[line.replace(b'\\u4e2d\\u6587','中文'.encode()).replace(b'\\ud83d\\ude80','🚀'.encode()) for line in data]
                    with mock.patch.object(wb,'open_upstream') as opened:h=self.invoke(api,http_stream(data,size))
                    opened.assert_not_called()
                    self.assertIn('中文 evidence. 🚀',self.thought(h))
                    self.assertIn('answer' if finish=='stop' else 'lookup',self.output(h))

    def test_heartbeat_passes_before_held_thought_without_flushing(self):
        data=frames(WIRE,text='Evidence.')+[HEART]+turn({'content':'answer'})
        guard=wb._GuardedStream(Stream(data),{'model':'deepseek-v4.1-flash','generation_deadline':None})
        try:
            iterator=iter(guard)
            self.assertEqual(next(iterator),HEART)
            rest=list(iterator)
            self.assertEqual(rest,[data[0]]+data[2:])
        finally:guard.close()
        self.assertEqual(self.counts().get('reasoning_buffer_neutral_heartbeat'),1)
        self.assertNotIn('reasoning_buffer_release_metadata',self.counts())

    def test_separators_and_comments_do_not_flood_recent_events(self):
        data=[]
        for i in range(40):data += frames(WIRE,text=f'Observation {i}.')+[HEART,b'\n']
        data+=turn({'content':'answer'})
        with mock.patch.object(wb.time,'monotonic',return_value=0):self.raw(data)
        self.assertEqual(self.counts().get('reasoning_buffer_entered'),1)
        self.assertEqual(self.counts().get('reasoning_buffer_neutral_heartbeat'),1)
        self.assertEqual(self.counts().get('reasoning_buffer_neutral_separator'),1)
        self.assertNotIn('reasoning_buffer_release_metadata',self.counts())
        self.assertLess(len(wb.loop_health_snapshot()['recent']),12)

    def test_neutral_frames_honor_time_bytes_and_frame_caps(self):
        for neutral in (b'\n',HEART):
            self.clear_events();data=frames(WIRE,text='Evidence.')+[neutral]+frames(WIRE,text='Next.')
            with mock.patch.object(wb.time,'monotonic',side_effect=[0,3,4]):self.assertEqual(self.raw(data),data)
            self.assertEqual(self.counts().get('reasoning_buffer_release_time'),1)
            self.assertEqual(self.counts().get('reasoning_buffer_entered'),1)
        for suffix,reason in (([b'\n']*520,'frames'),([b' '*65536+b'\n'],'bytes')):
            self.clear_events();data=frames(WIRE,text='Evidence.')+suffix
            with mock.patch.object(wb.time,'monotonic',return_value=0):self.assertEqual(self.raw(data),data)
            self.assertEqual(self.counts().get('reasoning_buffer_release_'+reason),1)

    def test_real_metadata_and_output_boundaries_still_flush(self):
        for boundary,reason in ((META,'metadata'),(chunk({'content':'answer'}),'content'),(chunk(GOOD_CALL),'tool')):
            self.clear_events();data=frames(WIRE,text='Evidence.')+[b'\n',boundary]
            self.assertEqual(self.raw(data),data)
            self.assertEqual(self.counts().get('reasoning_buffer_release_'+reason),1)

    def test_transport_failure_flushes_and_never_silently_succeeds(self):
        data=frames(WIRE,text='Evidence.')+[b'\n']
        guard=wb._GuardedStream(Stream(data,error=OSError('disconnect')),{'model':'deepseek-v4.1-flash','generation_deadline':None})
        try:
            it=iter(guard);self.assertEqual(next(it),data[0]);self.assertEqual(next(it),b'\n')
            with self.assertRaises(OSError):next(it)
        finally:guard.close()
        self.assertEqual(self.counts().get('reasoning_buffer_release_transport_error'),1)

    def test_eof_done_timeout_and_malformed_frames_preserve_failure_boundaries(self):
        for ending,reason in (([], 'eof'),([b'data: [DONE]\n\n'],'terminal'),([b'data: {invalid\n'],'protocol')):
            self.clear_events();data=frames(WIRE,text='Evidence.')+[b'\n']+ending
            self.assertEqual(self.raw(data),data)
            self.assertEqual(self.counts().get('reasoning_buffer_release_'+reason),1)
        guard=wb._GuardedStream(Stream(frames(WIRE,text='Evidence.')+[b'\n',HEART]),{'model':'deepseek-v4.1-flash','generation_deadline':None})
        try:
            with mock.patch.object(guard,'_timed_out',side_effect=[False,False,True]):out=b''.join(guard)
        finally:guard.close()
        self.assertIn(b'generation_timeout',out);self.assertIn(b'Evidence.',out)

    def test_second_loop_and_real_tool_output_never_add_third_attempt(self):
        for api in MODES:
            with mock.patch.object(wb.time,'monotonic',return_value=0),mock.patch.object(wb,'open_upstream',return_value=(http_stream(frames(WIRE,128)),Account('b'))) as opened:
                h=self.invoke(api,http_stream(frames(WIRE,128)))
            self.assertEqual(opened.call_count,1)
            self.assertIn('reasoning_loop_detected',self.output(h));self.assertNotIn('Let me run',self.output(h))
            with mock.patch.object(wb,'open_upstream') as opened:self.invoke(api,http_stream([chunk(GOOD_CALL)]+frames(WIRE,128)))
            opened.assert_not_called()

if __name__=='__main__':
    suite=unittest.TestSuite(SSEFramingTests(n) for n in SSEFramingTests.__dict__ if n.startswith('test_'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

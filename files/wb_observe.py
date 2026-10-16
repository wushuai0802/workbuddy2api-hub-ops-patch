"""Bounded, privacy-safe response observations; never changes model behavior.

Tool histories are analyzed within the current request only. No client session,
account, prompt, reasoning or tool text is persisted or shared across requests.
"""
from collections import Counter, deque
import contextvars
import functools
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid


CURRENT = contextvars.ContextVar('wb_ops_observation', default=None)
SIGNALS = frozenset(('reasoning_loop_detected', 'content_loop_detected', 'generation_timeout',
                     'upstream_sse_limit', 'upstream_choices_unsupported',
                     'upstream_candidate_unsupported', 'upstream_finish_error', 'progress_repeated_failure',
                     'progress_repeated_result', 'reasoning_stall_observed',
                     'retry_started', 'retry_recovered', 'retry_failed',
                     'retry_blocked_output_or_protocol', 'retry_budget_exhausted', 'retry_cooldown',
                     'retry_disabled', 'retry_open_failed', 'retry_hint_applied', 'retry_hint_skipped_shape'))
GUARD_KINDS = frozenset(('reasoning_loop_detected', 'content_loop_detected', 'generation_timeout',
                         'upstream_sse_limit', 'upstream_choices_unsupported', 'upstream_candidate_unsupported'))
GUARD_RULES = frozenset(('fast-period', 'fast-intent', 'exact', 'churn', 'mixed-stall'))
GUARD_METRICS = frozenset(('received_chars', 'window_chars', 'window_lines', 'intent_lines',
                          'dominant_count', 'distinct_intents', 'distinct_phrases', 'short_phrases',
                          'repeated_phrases', 'path', 'neutral_lines', 'neutral_dominant_count',
                          'substantive_lines', 'substantive_chars', 'content_seen', 'tool_seen', 'refusal_seen'))
OUTCOMES = frozenset(('completed', 'failed', 'client_aborted', 'upstream_aborted', 'unknown'))
REASONS = SIGNALS | frozenset(('none', 'upstream_stream_error', 'first_frame_probe_failed',
                             'upstream_truncated', 'reasoning_only_turn', 'empty_output_turn',
                             'continuation_timeout', 'continuation_request_failed',
                             'continuation_probe_failed', 'upstream_content_rejected',
                             'upstream_rate_limited', 'client_disconnect', 'other'))
KINDS = frozenset(('text', 'tool', 'refusal', 'reasoning', 'empty', 'unknown'))


def enabled():
    return os.environ.get('WB_OPS_OBSERVE', '1').lower() not in ('0', 'false', 'off')


def _number(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return min(9007199254740991, max(0, value))
    if isinstance(value, float) and math.isfinite(value):
        return min(9007199254740991, max(0, value))
    return 0


def _safe_name(value):
    value = str(value or '')
    return value if re.fullmatch(r'[a-zA-Z0-9_./:-]{1,96}', value) else 'unknown'


def reason_code(value):
    # Arbitrary exception strings never enter observation files.
    value = str(value or '')
    if value in REASONS:
        return value
    if value.startswith('first-frame probe failed'):
        return 'first_frame_probe_failed'
    for code in REASONS:
        if value.startswith(code+':'):
            return code
    return 'other'


def usage_fields(usage):
    usage = usage if isinstance(usage, dict) else {}
    output = {k: _number(usage.get(k)) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
    for field, detail, key in [('reasoning_tokens', 'completion_tokens_details', 'reasoning_tokens'),
                               ('cached_tokens', 'prompt_tokens_details', 'cached_tokens')]:
        value = usage.get(detail)
        output[field] = _number(value.get(key)) if isinstance(value, dict) else _number(usage.get(field))
    return output


def clean_guard_detail(value):
    if not isinstance(value, dict) or value.get('kind') not in GUARD_KINDS:
        return None
    detail = {'kind': value['kind'], 'rule': value.get('rule') if value.get('rule') in GUARD_RULES else 'unknown'}
    for key in GUARD_METRICS:
        if key in value and isinstance(value[key], int) and not isinstance(value[key], bool):
            detail[key] = int(_number(value[key]))
    return detail


def progress_signal(messages, model):
    """Report repeated paired results, not a claim that the task made no progress.

    Look at at most 96 messages, 32 paired calls and 64 KiB per result/arguments.
    Changed parameters remain part of evidence; polling/running is excluded.
    Hashes exist only during this invocation and never leave this function.
    """
    if str(model or '').strip().lower().rsplit('/', 1)[-1] != 'deepseek-v4.1-flash':
        return None
    if not isinstance(messages, list):
        return None
    tail = messages[-96:]
    for index in range(len(tail)-1, -1, -1):
        if isinstance(tail[index], dict) and tail[index].get('role') == 'user':
            tail = tail[index+1:]
            break
    calls, pairs = {}, deque(maxlen=32)
    for message in tail:
        if not isinstance(message, dict):
            continue
        if message.get('role') == 'assistant':
            tool_calls = message.get('tool_calls')
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls[:32]:
                if not isinstance(call, dict):
                    continue
                fn = call.get('function') or {}
                uid = call.get('id')
                if not isinstance(fn, dict) or not isinstance(uid, str) or len(uid) > 256:
                    continue
                arguments = fn.get('arguments')
                if not isinstance(arguments, str) or len(arguments) > 65536:
                    continue
                name = fn.get('name')
                if not isinstance(name, str) or len(name) > 128:
                    continue
                calls[uid] = (name, hashlib.sha256(arguments.encode()).digest())
                if len(calls) > 32:
                    calls.pop(next(iter(calls)))
        elif message.get('role') == 'tool':
            uid = message.get('tool_call_id')
            if not isinstance(uid, str):
                continue
            call = calls.pop(uid, None)
            content = message.get('content')
            if call is None or not isinstance(content, str) or len(content) > 65536:
                continue
            try:
                value = json.loads(content)
            except (ValueError, RecursionError):
                value = None
            running = (call[0].rsplit('.', 1)[-1] in ('write_stdin', 'wait', 'poll') or
                       'Process running with session ID' in content or
                       (isinstance(value, dict) and (value.get('status') in ('running', 'pending', 'in_progress') or
                        (value.get('session_id') is not None and value.get('exit_code') is None))))
            if running:
                pairs.clear()
                continue
            failed = bool(isinstance(value, dict) and (value.get('error') or value.get('success') is False or
                          value.get('status') in ('failed', 'error') or
                          (isinstance(value.get('exit_code'), int) and value['exit_code'] != 0)))
            failed = failed or bool(re.search(r'(?im)^\s*(?:error\b|failed\b|permission denied\b|not found\b|timeout\b)', content))
            # Ignore timing wrappers, but keep exit status, session/status and
            # substantive output. Numeric/new-result changes must remain visible.
            normalized = '\n'.join(line.rstrip() for line in content.replace('\r\n', '\n').splitlines()
                                   if not re.match(r'^(?:Chunk ID:|Wall time:|Total output lines:)', line)).strip()
            if not normalized:
                pairs.clear()
                continue
            pairs.append((call[0], call[1], hashlib.sha256(normalized.encode()).digest(), failed))
    recent = list(pairs)[-12:]
    if len(recent) < 8:
        return None
    results = Counter((name, digest, failed) for name, _, digest, failed in recent)
    (name, digest, failed), dominant = results.most_common(1)[0]
    if dominant < 8 or dominant * 4 < len(recent) * 3:
        return None
    return {'kind': 'progress_repeated_failure' if failed else 'progress_repeated_result',
            'paired_rounds': len(recent), 'repeated_results': dominant,
            'distinct_arguments': len({args for n, args, d, _ in recent if n == name and d == digest})}


def clean_record(row):
    """Validate both locally generated and on-disk rows against a fixed schema."""
    if not isinstance(row, dict) or row.get('schema') != 1 or row.get('phase') not in ('request', 'attempt'):
        return None
    if not re.fullmatch(r'[0-9a-f]{32}', str(row.get('request_id', ''))):
        return None
    result = {'schema': 1, 'phase': row['phase'], 'request_id': row['request_id'],
              'at': _number(row.get('at')), 'started': _number(row.get('started')),
              'patch_id': _safe_name(row.get('patch_id')), 'model': _safe_name(row.get('model')),
              'fingerprint': row.get('fingerprint') if re.fullmatch(r'[0-9a-f]{64}', str(row.get('fingerprint', ''))) else None,
              'protocol': row.get('protocol') if row.get('protocol') in ('chat', 'responses', 'messages') else 'unknown',
              'stream': bool(row.get('stream')), 'session_present': bool(row.get('session_present')),
              'outcome': row.get('outcome') if row.get('outcome') in OUTCOMES else 'unknown',
              'reason': reason_code(row.get('reason')),
              'final_kind': row.get('final_kind') if row.get('final_kind') in KINDS else 'unknown',
              'usage_source': row.get('usage_source') if row.get('usage_source') in ('provider', 'estimated', 'missing') else 'missing',
              'usage': usage_fields(row.get('usage')),
              'signals': {k: int(_number(v)) for k, v in (row.get('signals') or {}).items() if k in SIGNALS}
                         if isinstance(row.get('signals'), dict) else {}}
    for field in ('attempt', 'attempts', 'attempts_overflow', 'failed_attempts', 'recovery_with_output',
                  'paired_rounds', 'repeated_results', 'distinct_arguments'):
        if field in row:
            result[field] = int(_number(row[field]))
    details = row.get('guard_details')
    result['guard_details'] = [clean for value in details[-8:] if (clean := clean_guard_detail(value))] if isinstance(details, list) else []
    return result


class ObservationStore:
    """Bounded rotating JSONL; reads expose only validated numeric observations."""
    def __init__(self, path, max_bytes=10*1024*1024, backups=2):
        self.path = os.fspath(path)
        self.max_bytes, self.backups = max_bytes, backups
        self.lock = threading.Lock()
        self.write_failures = 0

    def append(self, rows):
        if not enabled():
            return
        lines = []
        for row in rows:
            clean = clean_record(row)
            if clean:
                lines.append((json.dumps(clean, separators=(',', ':')) + '\n').encode())
        if not lines:
            return
        data = b''.join(lines)
        with self.lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                paths = [self.path] + [self.path+'.'+str(i) for i in range(1, self.backups+1)]
                if any(os.path.islink(path) for path in paths):
                    self.write_failures += 1
                    return
                if os.path.exists(self.path) and os.path.getsize(self.path)+len(data) > self.max_bytes:
                    for i in range(self.backups, 0, -1):
                        source = paths[i-1]
                        if os.path.exists(source):
                            os.replace(source, paths[i])
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, 'ab') as sink:
                    sink.write(data)
            except OSError:
                # Observability failure never changes a successful generation.
                self.write_failures += 1
                return

    def snapshot(self, since=None, until=None, model=None):
        rows = deque(maxlen=20000)
        truncated = False
        read_errors, invalid_rows = 0, 0
        with self.lock:
            for suffix in [('.'+str(i)) for i in range(self.backups, 0, -1)] + ['']:
                path = self.path+suffix
                try:
                    if os.path.islink(path):
                        continue
                    # Only the bounded trailing region of an externally enlarged file.
                    with open(path, 'rb') as source:
                        size = os.fstat(source.fileno()).st_size
                        if size > self.max_bytes:
                            source.seek(size-self.max_bytes)
                            source.readline()
                            truncated = True
                        for line in source:
                            if len(line) > 16384:
                                truncated = True
                                invalid_rows += 1
                                continue
                            try:
                                row = clean_record(json.loads(line))
                            except (ValueError, TypeError):
                                invalid_rows += 1
                                continue
                            if row:
                                if len(rows) == rows.maxlen:
                                    truncated = True
                                rows.append(row)
                            else:
                                invalid_rows += 1
                except FileNotFoundError:
                    continue
                except (OSError, ValueError):
                    read_errors += 1
                    continue
        selected = [r for r in rows if (since is None or r['at'] >= since) and
                    (until is None or r['at'] <= until) and (not model or r['model'] == model)]
        groups = {}
        for row in selected:
            key = (row['patch_id'], row['fingerprint'], row['model'], row['protocol'], row['phase'])
            if key not in groups and len(groups) >= 64:
                key = ('other', None, 'other', 'unknown', row['phase'])
            group = groups.setdefault(key, {'rows': 0, 'outcomes': Counter(), 'reasons': Counter(),
                                           'final_kinds': Counter(), 'usage_sources': Counter(),
                                           'signals': Counter(), 'usage': Counter(), 'recovery_with_output': 0})
            group['rows'] += 1
            group['outcomes'][row['outcome']] += 1
            group['reasons'][row['reason']] += 1
            group['final_kinds'][row['final_kind']] += 1
            group['usage_sources'][row['usage_source']] += 1
            group['signals'].update(row['signals'])
            group['usage'].update(row['usage'])
            group['recovery_with_output'] += row.get('recovery_with_output', 0)
        return {'schema': 1, 'scope': 'response_adapters', 'business_completion': 'unmeasured',
                'write_enabled': enabled(), 'write_failures_this_process': self.write_failures,
                'read_errors': read_errors, 'invalid_rows': invalid_rows,
                'since': since, 'until': until, 'model': _safe_name(model) if model else None,
                'retention_max_bytes': self.max_bytes*(self.backups+1), 'scan_max_rows': rows.maxlen,
                'retained_rows_scanned': len(rows), 'truncated': truncated,
                'retained_since': min((r['at'] for r in rows), default=None),
                'retained_until': max((r['at'] for r in rows), default=None),
                'groups': [dict(patch_id=k[0], fingerprint=k[1], model=k[2], protocol=k[3], phase=k[4], **v)
                           for k, v in groups.items()], 'recent': selected[-64:]}


class RequestObservation:
    """At most 32 attempt records per adapter request, emitted once at exit."""
    def __init__(self, model, protocol, stream, patch_id, fingerprint, session_present=False):
        self.base = {'schema': 1, 'request_id': uuid.uuid4().hex, 'started': time.time(),
                     'model': model, 'protocol': protocol, 'stream': stream,
                     'patch_id': patch_id, 'fingerprint': fingerprint, 'session_present': session_present}
        self.attempts, self.overflow, self.signals = [], 0, Counter()
        self.current = None
        self.last_outcome, self.last_reason = 'unknown', 'none'
        self.client_aborted = False
        self.progress = {}

    def begin(self, holder):
        self.base['session_present'] = self.base['session_present'] or bool(holder.get('session_key'))
        if self.current is not None and not self.current.get('settled'):
            self.current.update(outcome='failed', reason='upstream_stream_error')
        row = dict(self.base, phase='attempt', started=time.time(), attempt=len(self.attempts)+self.overflow+1,
                   outcome='unknown', reason='none', final_kind='empty', usage=None,
                   usage_source='missing', signals=Counter(), guard_details=[])
        self.current = row
        if len(self.attempts) < 32:
            self.attempts.append(row)
        else:
            self.overflow += 1

    def saw(self, delta):
        if self.current is None:
            return
        calls = delta.get('tool_calls')
        calls = calls if isinstance(calls, list) else []
        legacy = delta.get('function_call')
        legacy = legacy if isinstance(legacy, dict) else {}
        tool = bool(legacy.get('name') or legacy.get('arguments')) or any(
            isinstance(c, dict) and (c.get('id') or (isinstance(c.get('function'), dict) and
                (c['function'].get('name') or c['function'].get('arguments')))) for c in calls)
        kind = ('tool' if tool else
                'refusal' if delta.get('refusal') else 'text' if delta.get('content') else
                'reasoning' if delta.get('reasoning_content') else None)
        if kind and (self.current['final_kind'] in ('empty', 'reasoning') or kind in ('tool', 'refusal')):
            self.current['final_kind'] = kind

    def usage(self, value, source='provider'):
        if self.current is not None and isinstance(value, dict):
            self.current['usage'], self.current['usage_source'] = value, source

    def signal(self, kind, metrics=None, rule=None, chars=None):
        if kind not in SIGNALS:
            return
        self.signals[kind] += 1
        if self.current is not None:
            self.current['signals'][kind] += 1
            if kind in GUARD_KINDS:
                detail = dict(metrics or {}, kind=kind, rule=rule)
                if chars is not None:
                    detail['received_chars'] = chars
                clean = clean_guard_detail(detail)
                if clean:
                    self.current['guard_details'] = (self.current['guard_details'] + [clean])[-4:]
        if kind.startswith('progress_') and isinstance(metrics, dict):
            self.progress = {k: int(_number(metrics.get(k))) for k in
                             ('paired_rounds', 'repeated_results', 'distinct_arguments')}

    def finish_attempt(self, outcome, usage=None, reason='none'):
        if self.current is None:
            self.begin({})
        if self.current['usage_source'] != 'provider' and isinstance(usage, dict):
            self.usage(usage, 'estimated')
        if reason == 'none' and outcome in ('failed', 'upstream_aborted') and self.current['guard_details']:
            reason = self.current['guard_details'][-1]['kind']
        self.current.update(outcome=outcome if outcome in OUTCOMES else 'unknown',
                            reason=reason_code(reason), settled=True)
        self.last_outcome, self.last_reason = self.current['outcome'], self.current['reason']

    def records(self):
        now = time.time()
        for row in self.attempts:
            row['at'] = now
        last = self.current or {}
        outcome = 'client_aborted' if self.client_aborted else (self.last_outcome if self.last_outcome != 'unknown' else 'failed')
        recovered = (self.signals['retry_started'] > 0 and outcome == 'completed' and
                     last.get('final_kind') in ('text', 'tool', 'refusal'))
        source = ('provider' if self.attempts and all(r['usage_source'] == 'provider' for r in self.attempts) else
                  'estimated' if any(r['usage'] for r in self.attempts) else 'missing')
        usage = Counter()
        for row in self.attempts:
            usage.update(usage_fields(row['usage']))
        request = dict(self.base, phase='request', at=now, outcome=outcome, reason='client_disconnect' if self.client_aborted else self.last_reason,
                       final_kind=last.get('final_kind', 'unknown'), signals=self.signals,
                       attempts=len(self.attempts)+self.overflow, attempts_overflow=self.overflow,
                       failed_attempts=sum(r['outcome'] in ('failed', 'upstream_aborted') for r in self.attempts),
                       recovery_with_output=int(recovered), usage=usage, usage_source=source,
                       guard_details=[d for a in self.attempts for d in a['guard_details']][-8:], **self.progress)
        return self.attempts+[request]


def tracked_response(protocol, stream, store_factory, identity):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(handler, upstream, model, *args, **kwargs):
            if not enabled() or CURRENT.get() is not None:
                return function(handler, upstream, model, *args, **kwargs)
            patch_id, fingerprint = identity()
            observation = RequestObservation(model, protocol, stream, patch_id, fingerprint,
                                             bool(kwargs.get('session_key')))
            token = CURRENT.set(observation)
            try:
                return function(handler, upstream, model, *args, **kwargs)
            except BaseException:
                observation.finish_attempt('upstream_aborted', reason='upstream_stream_error')
                raise
            finally:
                CURRENT.reset(token)
                store_factory().append(observation.records())
        return wrapped
    return decorate

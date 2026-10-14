"""Apply the bundle in a disposable upstream copy and run focused offline suites.

Fetching the pinned source is separate. Test children deny non-loopback sockets
and use empty account/usage paths. No model request or real credentials are used.
"""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apply import Bundle

PATCH_SUITES = ('test_installer.py', 'test_wb_stall.py', 'test_mixed_stall_proxy.py', 'test_keepalive_guard.py',
                'test_auth_deadline.py', 'test_proxy_boundaries.py', 'test_leaked_mixed_stall.py',
                'test_guarded_sse_batching.py', 'test_review_fixes.py', 'test_progress_observe.py', 'test_v1617.py', 'test_v172.py',
                'test_build_identity.py', 'test_dated_guards.py', 'test_observability.py',
                '_test_401_refresh.py', '_test_deepseek_multimodal.py', '_test_deepseek_reasoning_compaction.py',
                'test_integration.py', 'test_loop_recovery.py', 'test_reasoning_recovery.py',
                'test_nullable_buffer.py', 'test_metadata_buffer.py', 'test_compact_buffer.py',
                'test_extra_fields.py', 'test_sse_framing.py', 'test_buffer_records.py', 'test_recovery_hint.py')
UPSTREAM_SUITES = ('_test_upstream_repairs.py', '_test_namespace_tools.py', '_test_reasoning_backfill.py',
                   '_test_anthropic_messages.py', '_test_tool_choice_none.py',
                   '_test_upstream_pool.py', '_test_remaining_usage.py', '_test_streak_report.py',
                   '_test_hotpath_savings.py', '_test_upstream_timeouts.py', '_test_growth_prereq.py',
                   '_test_http_limits.py', '_test_local_web_tools.py', '_test_analytics_by_key.py',
                   '_test_anthropic_http.py', '_test_sse_read_batching.py', '_test_shared_ssl_context.py',
                   '_test_dsml_chat_fallback.py', '_test_custom_tools.py',
                   '_test_stop_normalization.py', '_test_pool_backoff.py', '_test_egress_backoff.py',
                   '_test_manual_weight.py', '_test_account_note.py', '_test_daily_chat.py',
                   '_test_closed_window_buckets.py', '_test_cold_window_scan.py',
                   '_test_pricing_used_fingerprint.py', '_test_usage_bytes_prefilter.py', '_test_recent_index.py')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--upstream', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / '.work/verification')
    args = parser.parse_args()
    upstream = args.upstream.resolve(strict=True)
    bundle = Bundle(ROOT)
    assert set(bundle.status(bundle.target(upstream)).values()) == {'base'}, 'use clean pinned upstream'
    args.output.mkdir(parents=True, exist_ok=True)
    run = args.output.resolve() / ('run-' + uuid.uuid4().hex[:12])
    run.mkdir()
    gateway, state = run / 'gateway', run / 'state'
    state.mkdir()
    shutil.copytree(upstream, gateway, ignore=shutil.ignore_patterns(
        '.git', '__pycache__', 'accounts', 'usage', '.ops-patch-backups', '.ops-patch-state.json', '.ops-patch.lock'))
    bundle.apply(gateway)
    for path in gateway.glob('wb_*.py'):
        ast.parse(path.read_text(encoding='utf8'), filename=path.name)
    env = dict(os.environ)
    env.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', WB_OPS_OFFLINE='1',
               WB_TEST_SOURCE=str(gateway), WB_OPS_TEST_STATE=str(state),
               ACCOUNTS_DIR=str(state / 'accounts'), WB_PROXY_USAGE_DIR=str(state / 'usage'),
               USAGE_DIR=str(state / 'usage'), WB_USAGE_DIR=str(state / 'usage'),
               API_KEY='', WB_PROXY_KEY='', WB_OPS_ALLOW_PROMPT_DEGRADE='0', WB_OPS_LOOP_RECOVERY_HINT='0')
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env.pop(name, None)
    for name in ('WB_SEARCH_ENDPOINT', 'WB_OPS_BING_FALLBACK'):
        env.pop(name, None)
    env['NO_PROXY'] = '127.0.0.1,localhost,::1'
    env['PYTHONPATH'] = os.pathsep.join((str(ROOT / 'tests/safety'), str(gateway), str(ROOT / 'tests')))
    rows = []
    suites = [ROOT / 'tests' / name for name in PATCH_SUITES]
    suites += [gateway / 'tests' / name for name in UPSTREAM_SUITES]
    for path in suites:
        started = time.monotonic()
        logfile = run / (path.stem + '.log')
        suite_env = dict(env, WB_OPS_LOOP_RETRY='0' if path.name == 'test_integration.py' else '1')
        with logfile.open('w', encoding='utf8') as output:
            result = subprocess.run([sys.executable, '-B', str(path)], cwd=gateway, env=suite_env,
                                    stdout=output, stderr=subprocess.STDOUT, timeout=180)
        text = logfile.read_text(encoding='utf8', errors='replace')
        counts = re.findall(r'Ran (\d+) tests?', text)
        row = {'suite': path.name, 'passed': result.returncode == 0,
               'seconds': round(time.monotonic() - started, 2)}
        if counts:
            row['tests'] = int(counts[-1])
        rows.append(row)
        print(json.dumps(row), flush=True)
        if result.returncode:
            print('\n'.join(text.splitlines()[-55:]), flush=True)
    report = {'patch_id': bundle.meta['patch_id'], 'passed': all(row['passed'] for row in rows),
              'suites': rows, 'model_requests': 0, 'data': 'synthetic fixtures only',
              'network': 'non-loopback socket access denied in test children', 'python': sys.version.split()[0]}
    (run / 'summary.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    print(json.dumps({'passed': report['passed'], 'suites': len(rows), 'report': str(run / 'summary.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

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
from scripts.offline_fixtures import prepare_test_fixtures

PATCH_SUITES = ('test_installer.py', 'test_wb_stall.py', 'test_mixed_stall_proxy.py', 'test_keepalive_guard.py',
                'test_auth_deadline.py', 'test_proxy_boundaries.py', 'test_leaked_mixed_stall.py',
                'test_guarded_sse_batching.py', 'test_review_fixes.py', 'test_progress_observe.py', 'test_phase_recovery.py', 'test_v1617.py', 'test_v172.py',
                'test_build_identity.py', 'test_dated_guards.py', 'test_observability.py', 'test_logstore.py',
                '_test_401_refresh.py', '_test_deepseek_multimodal.py', '_test_deepseek_reasoning_compaction.py',
                'test_integration.py', 'test_loop_recovery.py', 'test_reasoning_recovery.py',
                'test_nullable_buffer.py', 'test_metadata_buffer.py', 'test_compact_buffer.py',
                'test_extra_fields.py', 'test_sse_framing.py', 'test_buffer_records.py', 'test_recovery_hint.py', 'test_v176.py', 'test_v178.py', 'test_v1710.py', 'test_stream_contract.py', 'test_v1712.py', 'test_v1715.py',
                'test_printed_call_boundaries.py', 'test_v1728.py', 'test_realm_fallback_boundaries.py', 'test_v1730.py',
                'test_account_slot_lifecycle.py', 'test_heartbeat_lifecycle.py', 'test_connection_deadline.py', 'test_v1733.py')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--upstream', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / '.work/verification')
    args = parser.parse_args()
    upstream = args.upstream.resolve(strict=True)
    official_suites = sorted((upstream / 'tests').glob('_test_*.py'))
    assert len(official_suites) == 166, 'pinned official Python inventory changed'
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
    prepare_test_fixtures(gateway)
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
    for name in ('WB_SEARCH_ENDPOINT', 'WB_OPS_BING_FALLBACK',
                 'WB_BACKGROUND_MODEL_NAMES', 'WB_BACKGROUND_MODEL_TARGET', 'WB_ENABLE_FREE_MODEL_CREDIT_FALLBACK',
                 'WB_CN_CHAT_UPSTREAM'):
        env.pop(name, None)
    env['NO_PROXY'] = '127.0.0.1,localhost,::1'
    env['PYTHONPATH'] = os.pathsep.join((str(ROOT / 'tests/safety'), str(gateway), str(ROOT / 'tests')))
    rows = []
    suites = [ROOT / 'tests' / name for name in PATCH_SUITES]
    suites += [gateway / 'tests' / path.name for path in official_suites]
    for path in suites:
        started = time.monotonic()
        logfile = run / (path.stem + '.log')
        suite_env = dict(env, WB_OPS_LOOP_RETRY='0' if path.name == 'test_integration.py' else '1')
        # Exercise the native opt-in prompt replacement contracts explicitly;
        # our safe-default suites and deployment policy keep it disabled.
        if path.parent == gateway / 'tests' and path.name in ('_test_prompt_modes.py', '_test_restricted_403.py'):
            suite_env['WB_OPS_ALLOW_PROMPT_DEGRADE'] = '1'
        # Existing six-path protocol contracts exercise the explicit opt-in.
        # Native/default-policy suites and v176 contracts control their own gate.
        suite_env['WB_ENABLE_ANTHROPIC_MESSAGES'] = '1' if path.parent == ROOT / 'tests' and path.name != 'test_v176.py' else ''
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
              'network': 'non-loopback socket access denied in test children', 'python': sys.version.split()[0],
              'patch_suites': len(PATCH_SUITES), 'official_python_suites': len(official_suites),
              'packaging_validation': 'isolated patched manifest and stdlib fixture; no portable runtime built'}
    (run / 'summary.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    print(json.dumps({'passed': report['passed'], 'suites': len(rows), 'report': str(run / 'summary.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

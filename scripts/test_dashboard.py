"""Run pinned native and patch Node suites against the installed dashboard."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apply import Bundle
from scripts.offline_fixtures import prepare_dashboard_fixtures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--upstream', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / '.work/dashboard')
    parser.add_argument('--node', default='node')
    args = parser.parse_args()
    upstream = args.upstream.resolve(strict=True)
    bundle = Bundle(ROOT)
    assert set(bundle.status(bundle.target(upstream)).values()) == {'base'}
    run = args.output.resolve() / ('run-' + uuid.uuid4().hex[:12])
    run.mkdir(parents=True)
    gateway = run / 'gateway'
    gateway.mkdir()
    (gateway / 'dashboard.html').write_bytes(bundle.payload.get('dashboard.html', (upstream / 'dashboard.html').read_bytes()))
    shutil.copytree(upstream / 'tests', gateway / 'tests', ignore=shutil.ignore_patterns('__pycache__'))
    suites = sorted((gateway / 'tests').glob('_test_*.js'))
    assert len(suites) == 50, 'pinned dashboard suite inventory changed'
    prepare_dashboard_fixtures(gateway)
    suites.append(ROOT / 'tests/test_dashboard_controls.js')
    suites.append(ROOT / 'tests/test_dashboard_ranges.js')
    env = {key: val for key, val in os.environ.items()
           if key.upper() not in ('API_KEY', 'WB_PROXY_KEY', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NODE_OPTIONS')
           and not key.startswith('WB_')}
    env['WB_OPS_DASHBOARD_ROOT'] = str(gateway)
    rows = []
    for path in suites:
        log = run / (path.stem + '.log')
        with log.open('w', encoding='utf8') as output:
            result = subprocess.run([args.node, str(path)], cwd=gateway, env=env,
                                    stdout=output, stderr=subprocess.STDOUT, timeout=45)
        rows.append({'suite': path.name, 'passed': result.returncode == 0})
        if result.returncode:
            print(log.read_text(encoding='utf8', errors='replace')[-4000:])
    report = {'passed': all(row['passed'] for row in rows), 'patch_id': bundle.meta['patch_id'],
              'suites': rows, 'model_requests': 0, 'official_suites': 50, 'patch_suites': 2,
              'data': 'synthetic DOM/fetch fixtures with the installed patched dashboard'}
    (run / 'summary.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    print(json.dumps({'passed': report['passed'], 'suites': len(rows), 'report': str(run / 'summary.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

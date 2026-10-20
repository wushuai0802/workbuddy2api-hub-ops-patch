"""Regenerate the exact-baseline metadata, reviewable diff and package checksums."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMMIT = 'af706fc31e4a22cb2dea7442361b2b15185c3cee'
TAG = 'v1.7.17'
PATCH_ID = 'v1.7.17-ops.20261020.1'
PATCH_SOURCE = 'wushuai0802/workbuddy2api-hub-ops-patch'
MODULES = ('dashboard.html', 'wb_proxy.py', 'wb_webtools.py', 'wb_accounts.py', 'wb_ops.py', 'wb_stall.py', 'wb_upstream_pool.py', 'wb_observe.py')
PUBLIC_DIRS = ('files', 'tests', 'docs', 'scripts', 'patches', '.github')
PUBLIC_ROOT = ('README.md', 'README.en.md', 'LICENSE', 'NOTICE.md', 'THIRD-PARTY-zh.md', 'CHANGELOG.md',
               'release-notes.md', '.gitignore', '.gitattributes', 'apply.py', 'BASELINE.json')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalize(path):
    return path.read_bytes().replace(b'\r\n', b'\n')


def public_files(root=ROOT):
    selected = [root / name for name in PUBLIC_ROOT if (root / name).is_file()]
    for name in PUBLIC_DIRS:
        directory = root / name
        if not directory.exists():
            continue
        for path in directory.rglob('*'):
            if path.is_symlink():
                raise ValueError('symlink in package: ' + path.relative_to(root).as_posix())
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo'):
                selected.append(path)
    return sorted(selected, key=lambda path: path.relative_to(root).as_posix())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--upstream', type=Path, required=True)
    args = parser.parse_args()
    upstream = args.upstream.resolve(strict=True)
    actual = subprocess.check_output(['git', '-C', str(upstream), 'rev-parse', 'HEAD'], text=True).strip()
    assert actual == COMMIT, 'upstream must be the pinned commit'
    files, chunks = {}, []
    for name in MODULES:
        base = normalize(upstream / name) if (upstream / name).exists() else None
        payload = normalize(ROOT / 'files' / name)
        files[name] = {'base_sha256': digest(base) if base is not None else None,
                       'patched_sha256': digest(payload)}
        chunks.extend(difflib.unified_diff(
            base.decode('utf8').splitlines(keepends=True) if base is not None else [],
            payload.decode('utf8').splitlines(keepends=True),
            fromfile='a/' + name if base is not None else '/dev/null', tofile='b/' + name))
    dependencies = sorted([path.name for path in upstream.glob('wb_*.py') if path.name not in MODULES]
                          + [name for name in ('dashboard.html',) if name not in MODULES])
    payload_identity = hashlib.sha256(''.join(
        name + ':' + files[name]['patched_sha256'] + '\n' for name in sorted(files)
    ).encode('ascii')).hexdigest()
    metadata = {'schema_version': 1, 'patch_id': PATCH_ID, 'base_tag': TAG,
                'patch_source': PATCH_SOURCE, 'payload_fingerprint': payload_identity,
                'repository': 'https://github.com/ardeyouxipianyi/workbuddy2api-hub',
                'upstream_commit': COMMIT, 'python_requires': '>=3.11', 'files': files,
                'compatibility': {name: digest(normalize(upstream / name)) for name in dependencies}}
    (ROOT / 'BASELINE.json').write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\n', encoding='utf8')
    (ROOT / 'patches' / (PATCH_ID + '.patch')).write_text(''.join(chunks), encoding='utf8')
    # Git/source archives and release archives must have identical LF bytes.
    for path in public_files():
        original = path.read_bytes()
        fixed = original.replace(b'\r\n', b'\n')
        if original != fixed:
            path.write_bytes(fixed)
    lines = [digest(path.read_bytes()) + '  ' + path.relative_to(ROOT).as_posix()
             for path in public_files()]
    (ROOT / 'MANIFEST.sha256').write_bytes(('\n'.join(lines) + '\n').encode('utf8'))
    print(json.dumps({'patch_id': PATCH_ID, 'files': len(lines), 'compatibility_files': len(dependencies)}))


if __name__ == '__main__':
    main()

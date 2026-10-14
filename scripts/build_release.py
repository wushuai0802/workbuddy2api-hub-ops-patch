"""Build a checksummed ZIP from the explicit public manifest only."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apply import Bundle, safe_relative


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    bundle = Bundle(ROOT)
    args.output.mkdir(parents=True, exist_ok=True)
    filename = 'workbuddy2api-hub-v1.7.3-wushuai0802-ops-20261014-r1.zip'
    archive = args.output / filename
    members = [line.split('  ', 1)[1] for line in (ROOT / 'MANIFEST.sha256').read_text().splitlines()]
    members.append('MANIFEST.sha256')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name in sorted(members):
            path = ROOT / safe_relative(name)
            assert path.is_file() and not path.is_symlink()
            item = zipfile.ZipInfo('workbuddy2api-hub-ops-patch/' + name, (2026, 10, 14, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            output.writestr(item, path.read_bytes())
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (args.output / 'SHA256SUMS.txt').write_text(checksum + '  ' + filename + '\n', encoding='utf8')
    print(json.dumps({'patch_id': bundle.meta['patch_id'], 'archive': str(archive),
                      'sha256': checksum, 'members': len(members)}))


if __name__ == '__main__':
    main()

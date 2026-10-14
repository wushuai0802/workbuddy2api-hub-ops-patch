"""Release identity must match the installed payload, independent of filenames."""
import ast
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_integration import wb


ROOT = Path(__file__).resolve().parents[1]


class BuildIdentityTests(unittest.TestCase):
    def test_running_payload_matches_release_metadata(self):
        metadata = json.loads((ROOT / 'BASELINE.json').read_text(encoding='utf8'))
        with mock.patch.object(wb, 'USAGE_LOG', '/not-present/synthetic-usage.jsonl'):
            health = wb.retry_health_snapshot()
        self.assertEqual(health['ops_patch_id'], metadata['patch_id'])
        self.assertEqual(health['ops_patch_source'], metadata['patch_source'])
        self.assertEqual(health['ops_patch_fingerprint'], metadata['payload_fingerprint'])
        self.assertRegex(health['ops_patch_fingerprint'], r'^[0-9a-f]{64}$')

    def identity_function(self, source):
        tree = ast.parse(Path(wb.__file__).read_text(encoding='utf8'))
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == '_patch_payload_fingerprint')
        namespace = {'__file__': str(source / 'wb_proxy.py'), 'hashlib': hashlib, 'os': wb.os}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<identity-only>', 'exec'), namespace)
        return namespace['_patch_payload_fingerprint']

    def test_same_version_different_payload_changes_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for name in ('wb_proxy.py', 'wb_accounts.py', 'wb_webtools.py', 'wb_ops.py', 'wb_stall.py', 'wb_upstream_pool.py'):
                (source / name).write_bytes(('# synthetic ' + name + '\n').encode())
            fingerprint = self.identity_function(source)
            before = fingerprint()
            (source / 'wb_stall.py').write_bytes(b'# a different detector, same declared version\n')
            self.assertNotEqual(fingerprint(), before)

    def test_crlf_checkout_is_same_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for name in ('wb_proxy.py', 'wb_accounts.py', 'wb_webtools.py', 'wb_ops.py', 'wb_stall.py', 'wb_upstream_pool.py'):
                (source / name).write_bytes(b'# synthetic\n')
            fingerprint = self.identity_function(source)
            before = fingerprint()
            for path in source.glob('*.py'):
                path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
            self.assertEqual(fingerprint(), before)

    def test_missing_source_does_not_claim_verified_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(self.identity_function(Path(directory))())


if __name__ == '__main__':
    unittest.main()

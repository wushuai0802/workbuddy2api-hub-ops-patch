"""Calendar-independent official assertions and fixed baseline inventory."""
import datetime
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.offline_fixtures import reset_fixture_clock, test_command
from scripts.generate_metadata import official_test_inventory


class MaintenanceTests(unittest.TestCase):
    def source(self, year):
        future = datetime.datetime(year, 5, 6, tzinfo=datetime.timezone.utc).timestamp()
        return (f'DETAIL_EN = "reset at {year}-05-06 00:00:00 UTC+0"\n'
                f'DETAIL_CN = "reset at {year}-05-06 08:00:00 UTC+8"\n'
                f'P.rate_limit_headers({int(future) + 600})\n'
                f'P.rate_limit_reset_note({int(future) + 600})\n'
                'def test_a_stale_wall_clock_falls_back_to_the_local_window():\n'
                f'    stale = "will reset at {year-2}-01-01 00:00:00 UTC+8"\n')

    def test_clock_follows_future_fixture_dates_without_a_fixed_day(self):
        for year in (2029, 2040, 2055):
            with self.subTest(year=year):
                expected = datetime.datetime(year, 5, 6, tzinfo=datetime.timezone.utc).timestamp() - 3600
                self.assertEqual(reset_fixture_clock(self.source(year)), expected)

    def test_missing_or_inconsistent_date_samples_fail_closed(self):
        with self.assertRaises(AssertionError):
            reset_fixture_clock('DETAIL_EN = "unknown new fixture"')
        with self.assertRaises(AssertionError):
            reset_fixture_clock(self.source(2030).replace('2028-01-01', '2031-01-01'))

    def test_date_suite_command_keeps_official_source_byte_identical(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '_test_rate_limit_reset_at.py'
            original = self.source(2033).encode('utf8')
            path.write_bytes(original)
            command = test_command(path)
            self.assertIn('runpy.run_path', command[3])
            self.assertEqual(path.read_bytes(), original)

    def test_inventory_binds_names_and_contents_to_the_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'tests').mkdir()
            path = root / 'tests/_test_example.py'; path.write_bytes(b'assert True\n')
            first = official_test_inventory(root)
            path.write_bytes(b'assert True\r\n')
            self.assertEqual(official_test_inventory(root), first)
            path.write_bytes(b'assert False\n')
            self.assertNotEqual(official_test_inventory(root), first)
            path.rename(root / 'tests/_test_renamed.py')
            self.assertNotEqual(official_test_inventory(root)['inventory_sha256'], first['inventory_sha256'])

    def test_native_dynamic_date_suite_runs_without_clock_adapter(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '_test_rate_limit_reset_at.py'
            original = b'SAMPLE_RESET_AT = int(time.time()) + SAMPLE_RESET_IN\n'
            path.write_bytes(original)
            self.assertEqual(test_command(path), [sys.executable, '-B', str(path)])
            self.assertEqual(path.read_bytes(), original)
            path.write_text('SAMPLE_RESET_AT = unexpected_clock()\n')
            with self.assertRaises(AssertionError):
                test_command(path)


if __name__ == '__main__':
    unittest.main()

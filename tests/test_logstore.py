"""The newly required upstream log store stays opt-in and preserves its data."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from test_integration import wb


class LogStoreTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ['WB_OPS_TEST_STATE'])))
        self.path=self.root/'logs.sqlite3'
        self.store=wb.wb_logstore.LogStore(str(self.path))
        self.addCleanup(self.store.close)

    def row(self, index):
        return {'id':index,'ts':time.strftime('%Y-%m-%d %H:%M:%S'),'time':'12:00:00',
                'level':'ERROR' if index%2 else 'INFO','tag':'chat','msg':'synthetic log '+str(index)}

    def test_disabled_default_does_not_create_database(self):
        self.assertFalse(self.store.enabled)
        self.store.append(self.row(1))
        self.assertIsNone(self.store.query())
        self.assertFalse(self.path.exists())

    def test_enabled_persists_queries_and_disable_preserves_file(self):
        self.store.configure(True,max_rows=100,max_days=7)
        for i in range(1,6):self.store.append(self.row(i))
        result=self.store.query(limit=20,level='ERROR')
        self.assertEqual({r['id'] for r in result['logs']},{1,3,5})
        self.store.close()
        restarted=wb.wb_logstore.LogStore(str(self.path))
        self.addCleanup(restarted.close)
        restarted.configure(True,max_rows=100,max_days=7)
        self.assertEqual(restarted.max_id(),5)
        self.assertEqual(restarted.stats()['rows'],5)
        restarted.configure(False)
        self.assertTrue(self.path.exists())


if __name__=='__main__':unittest.main()

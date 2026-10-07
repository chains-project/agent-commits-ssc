"""Coverage checks must not turn partial records into complete release histories."""
import sqlite3
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from common import pack
from paper_freshness import Freshness


class FreshnessCoverageTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE observations(eco,pkg,body)')
        self.row = dict(final_queryable='1',ecosystem='npm',effective_package='x',
            query_kind='exact',matching_time='2020-01-01T00:00:00Z',
            matching_version='1.0.0',label='present_at_commit',time_type='npm_version_time')

    def tearDown(self):
        self.db.close()

    def add_record(self, scope, versions):
        self.db.execute('INSERT INTO observations VALUES(?,?,?)',
            ('npm','x',pack(dict(scope=scope,lookup_status='ok',versions=versions))))

    def test_partial_record_only_supports_selected_version(self):
        self.add_record('observed_subset',[dict(version='1.0.0',published_at='2020-01-01T00:00:00Z')])
        f = Freshness(self.db)
        f.add(self.row)
        c = f.counts['GLOBAL']
        self.assertEqual(c['exact_with_selected_time'],1)
        self.assertEqual(c['exact_with_enumeration'],0)

    def test_missing_date_prevents_fully_dated_list(self):
        self.add_record('enumeration',[dict(version='1.0.0',published_at='2020-01-01T00:00:00Z'),
                                       dict(version='2.0.0',published_at='')])
        f = Freshness(self.db)
        f.add(self.row)
        c = f.counts['GLOBAL']
        self.assertEqual(c['exact_with_enumeration'],1)
        self.assertEqual(c['exact_present_in_fully_dated_enumeration'],0)


if __name__ == '__main__':
    unittest.main()

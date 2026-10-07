"""Check interval boundaries and conservation of temporal positives."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from postdate_summary import interval_index, PostdateCounts


class PostdateTests(unittest.TestCase):
    def test_boundaries(self):
        gaps = ('0.001', '7200', '7200.001', '86400', '86400.001',
                '604800', '604800.001', '2592000', '2592000.001')
        self.assertEqual([interval_index(x) for x in gaps], [0,0,1,1,2,2,3,3,4])
        for value in ('0', '-1', 'NaN', 'Infinity', ''):
            with self.assertRaises(ValueError):
                interval_index(value)

    def test_conservation_and_cumulative(self):
        counts = PostdateCounts()
        for i, gap in enumerate(('3600', '86400', '604800', '2592000', '2592001')):
            counts.add(dict(label='hallucinated_dependency', subtype='postdate',
                boundary_source='committer', final_queryable='1', episode_id=str(i),
                ecosystem='npm', proxy_time=str(i == 0), delta_seconds=gap))
        records = [r for r in counts.records() if r['ecosystem'] == 'GLOBAL']
        self.assertEqual(sum(r['episodes'] for r in records), 5)
        self.assertEqual([r['cumulative_episodes'] for r in records], [1,2,3,4,''])
        self.assertEqual(sum(r['proxy_episodes'] for r in records), 1)


if __name__ == '__main__':
    unittest.main()

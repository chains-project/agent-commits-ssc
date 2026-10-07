"""Checks for overlapping agents, repository aliases and candidate-only labels."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from paper_counts import Counts, CURRENT, HISTORICAL


class PaperCountsTests(unittest.TestCase):
    def test_overlap_and_repository_union(self):
        c = Counts('test', CURRENT)
        member = dict(agents='claude|cursor', repository_key='github_id:1')
        c.add('c1','npm',1,'hallucinated_dependency',member,'postdate')
        c.add('c2','PyPI',1,'present_at_commit',member)
        c.add('c3','npm',0,'insufficient_evidence',dict(agents='claude',repository_key='github_id:2'))
        c.verify()
        g = c.records('global')[0]
        self.assertEqual((g['queryable'],g['queryable_repositories']),(2,1))
        self.assertEqual(g['queryable_insufficient_evidence'],0)
        self.assertEqual(g['insufficient_evidence'],1)
        self.assertEqual(sum(r['hallucinated_dependency'] for r in c.records('agent')),2)

    def test_unreviewed_is_separate(self):
        c = Counts('test',HISTORICAL)
        c.add('c','npm',1,'not_in_candidate_review',dict(agents='codex',repository_key='github_id:3'))
        c.verify()
        g = c.records('global')[0]
        self.assertEqual(g['not_in_candidate_review'],1)
        self.assertEqual(g['not_hallucination'],0)


if __name__ == '__main__':
    unittest.main()

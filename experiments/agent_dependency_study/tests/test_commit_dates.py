"""Date recovery and boundary decisions independent of the outcome label."""
import sys
import unittest
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(BASE),str(BASE/'src')]
from commit_dates import normalize_date,add_dates,resolve_dates
from stage3_run import select_boundary
from stage3_classify import decide,timestamp


class CommitDates(unittest.TestCase):
    def item(self):
        return dict(commit_id='c',repo='r',sha='s',raw_observations=1,
                    author_candidates={},committer_candidates={})

    def test_timezone_equivalence_is_not_conflict(self):
        row=self.item()
        add_dates(row,'2026-01-01T11:00:00+01:00','2026-01-01T12:00:00+01:00','a')
        add_dates(row,'2026-01-01T10:00:00Z','2026-01-01T11:00:00Z','b')
        result=resolve_dates(row)
        self.assertEqual(result['metadata_status'],'complete')
        self.assertEqual(result['committer'],'2026-01-01T11:00:00Z')

    def test_conflicting_committer_is_not_arbitrarily_selected(self):
        row=self.item()
        add_dates(row,'','2026-01-01T11:00:00Z','a')
        add_dates(row,'','2026-01-01T12:00:00Z','b')
        self.assertEqual(resolve_dates(row)['metadata_status'],'committer_conflict')
        self.assertEqual(resolve_dates(row)['committer'],'')

    def test_invalid_or_naive_time_is_missing(self):
        for value in ['','wrong','2026-01-01T12:00:00']:
            self.assertEqual(normalize_date(value),'')

    def test_missing_committer_does_not_fall_back(self):
        row=dict(committer='',author='2026-01-01T10:00:00Z')
        self.assertEqual(select_boundary(row,'committer'),('','missing'))
        self.assertEqual(select_boundary(row,'author'),(row['author'],'author'))

    def test_legacy_reproduction_keeps_its_boundary(self):
        row=dict(committer='',author='2026-01-01T10:00:00Z')
        self.assertEqual(select_boundary(row,'legacy'),(row['author'],'author_fallback'))

    def test_release_between_dates_changes_only_temporal_result(self):
        facts=dict(matches={'1':dict(times=[(timestamp('2026-01-01T10:30:00Z'),'npm_version_time','e')],evidence={'e'})},
                   uncertain_match=False,partial_match=False,enumerated_match=True)
        self.assertEqual(decide(facts,timestamp('2026-01-01T11:00:00Z'))['label'],'present_at_commit')
        self.assertEqual(decide(facts,timestamp('2026-01-01T10:00:00Z'))['subtype'],'postdate')
        self.assertEqual(decide(facts,None)['reason'],'commit_time_missing')

    def test_committer_is_not_assumed_later_than_author(self):
        row=dict(committer='2026-01-01T09:00:00Z',author='2026-01-01T10:00:00Z')
        self.assertEqual(select_boundary(row,'committer'),(row['committer'],'committer'))

    def test_package_absence_does_not_need_a_time_boundary(self):
        facts=dict(matches={},uncertain_match=False,negative=[('package','e')],package_exists=False,considered=['e'])
        self.assertEqual(decide(facts,None)['subtype'],'package_no_match')


if __name__=='__main__':unittest.main()

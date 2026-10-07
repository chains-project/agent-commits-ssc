"""Reproduce small paper tables from frozen results and complete agent membership.

Inputs are read-only. Keeps canonical C/P and current four-label results separate.
No classification, registry requests or bootstrap is performed.
"""
import argparse
import csv
import gzip
import json
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE/'src'))
from common import digest, read_db, write_json
from paper_counts import Counts, CURRENT, HISTORICAL
from paper_freshness import Freshness


def load_members():
    directory = BASE/'inputs/analysis_membership'
    manifest = json.loads((directory/'manifest.json').read_text())
    assert digest(directory/'commits.csv.gz') == manifest['sha256']
    with gzip.open(directory/'commits.csv.gz', 'rt', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    result = {r['commit_id']:r for r in rows}
    assert len(result) == len(rows) == manifest['commits']
    return result


def historical(db, members, output):
    result = Counts('canonical_v2_20260816', HISTORICAL)
    sql = '''SELECT e.cid,e.eco,e.q,l.label,c.repo,c.sha FROM episodes e
        JOIN commits c ON c.id=e.cid LEFT JOIN old_labels l ON l.eid=e.id'''
    for r in db.execute(sql):
        member = members[r['cid']]
        assert (r['repo'],r['sha']) == (member['repo'],member['sha'])
        result.add(r['cid'], r['eco'], r['q'], r['label'] or 'not_in_candidate_review', member)
    g = result.counts['global','GLOBAL','ALL']
    assert (g['queryable'],g['confirmed_hallucination'],g['probable_hallucination'],g['indeterminate']) == (1364856,322,382,52093)
    result.save(output, 'historical')
    return result


def current(db, members, output, summary):
    result = Counts(summary['result_version'], CURRENT)
    fresh = Freshness(db)
    seen_commits = set()
    with gzip.open(BASE/'results_integrated/episodes.csv.gz', 'rt', encoding='utf-8') as f:
        for i,r in enumerate(csv.DictReader(f),1):
            member = members[r['commit_id']]
            assert (r['repo'],r['sha']) == (member['repo'],member['sha'])
            q = int(r['final_queryable'])
            assert q == int(r['query_kind'] in {'exact','range','name_only'})
            result.add(r['commit_id'],r['ecosystem'],q,r['label'],member,r['subtype'])
            fresh.add(r)
            seen_commits.add(r['commit_id'])
            if i % 200000 == 0:
                print('Summarized',i,'current episodes',flush=True)
    g = result.counts['global','GLOBAL','ALL']
    assert g['episodes'] == summary['episodes'] and g['queryable'] == summary['queryable']
    assert all(g[k] == v for k,v in summary['labels'].items())
    assert seen_commits == set(members)
    result.save(output, 'current')
    fresh.save(output)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=BASE/'results_paper_tables')
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    sources = [BASE/'results_integrated/episodes.csv.gz',BASE/'results_integrated/summary.json',
               BASE/'inputs/observations.sqlite',BASE/'inputs/analysis_membership/commits.csv.gz',
               Path(__file__),BASE/'src/paper_counts.py',BASE/'src/paper_freshness.py']
    hashes = {p.relative_to(BASE).as_posix():digest(p) for p in sources}
    summary = json.loads((BASE/'results_integrated/summary.json').read_text())
    assert hashes['inputs/observations.sqlite'] == summary['registry_sha256']
    assert hashes['results_integrated/episodes.csv.gz'] == summary['output_sha256']['episodes.csv.gz']
    members = load_members()
    db = read_db(BASE/'inputs/observations.sqlite')
    print('Summarizing historical canonical labels',flush=True)
    old = historical(db,members,args.output)
    print('Summarizing current labels and saved release coverage',flush=True)
    new = current(db,members,args.output,summary)
    db.close()
    assert hashes == {p.relative_to(BASE).as_posix():digest(p) for p in sources}
    write_json(args.output/'PROVENANCE.json', dict(status='passed',inputs=hashes,
        result_version=summary['result_version'],population='expanded',
        denominator='exact/range/name_only episodes; complete qualifying agent membership',
        repository_population='distinct stable repository keys among queryable episodes in each stratum',
        repository_resolution=dict(Counter(r['repository_resolution'] for r in members.values())),
        validation='label/denominator totals, ecosystem partition, agent-cross-table margins and overlap excess checked',
        network=False,bootstrap=False,
        outputs={p.name:digest(p) for p in args.output.glob('*.csv')}))
    print('Completed small paper tables:',args.output,flush=True)


if __name__ == '__main__':
    main()

"""Check paper tables against prior published margins and independent SQL unions."""
import csv
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path
BASE = Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'src'))
from common import read_db, digest, write_json


def read(path):
    with path.open(encoding='utf-8',newline='') as f:
        return list(csv.DictReader(f))


def repository_unions(db_path, members):
    repos, commits = defaultdict(set), defaultdict(set)
    db = read_db(db_path)
    for row in db.execute('SELECT DISTINCT cid,eco FROM episodes WHERE q=1'):
        member = members[row['cid']]
        keys = [('global','GLOBAL','ALL'),('ecosystem','ALL',row['eco'])]
        for agent in member['agents'].split('|'):
            keys.extend([('agent',agent,'ALL'),('agent_ecosystem',agent,row['eco'])])
        for key in keys:
            repos[key].add(member['repository_key'])
            commits[key].add(row['cid'])
    db.close()
    return repos, commits


def main():
    directory = BASE/'results_paper_tables'
    manifest = json.loads((directory/'PROVENANCE.json').read_text())
    for name, sha in manifest['outputs'].items():
        assert digest(directory/name) == sha, name
    with gzip.open(BASE/'inputs/analysis_membership/commits.csv.gz','rt',encoding='utf-8') as f:
        members = {r['commit_id']:r for r in csv.DictReader(f)}
    for version, db in [('current','results_replay_v2/stage12.sqlite'),('historical','inputs/observations.sqlite')]:
        repos, commits = repository_unions(BASE/db,members)
        for dim in ['global','agent','ecosystem','agent_ecosystem']:
            for row in read(directory/(version+'_'+dim+'.csv')):
                key = dim,row['agent'],row['ecosystem']
                assert len(repos[key]) == int(row['queryable_repositories']),key
                assert len(commits[key]) == int(row['queryable_commits']),key
    expected = {'claude':(302449,49,100,11590),'codex':(22399,2,6,1643),
                'copilot':(509038,108,141,23062),'cursor':(532330,163,135,15852)}
    for row in read(directory/'historical_agent.csv'):
        assert tuple(int(row[k]) for k in ['queryable','confirmed_hallucination','probable_hallucination','indeterminate']) == expected[row['agent']]
    overlap = read(directory/'historical_overlap.csv')
    assert sum(int(r['episodes']) for r in overlap) == 2113
    assert sum(int(r['queryable']) for r in overlap) == 1360
    fresh = next(r for r in read(directory/'freshness_evidence_coverage.csv') if r['ecosystem']=='GLOBAL')
    assert int(fresh['exact']) == 419808
    time = json.loads((directory/'HISTORICAL_TIME_PROVENANCE.json').read_text())
    for name,sha in time['outputs'].items():
        assert digest(directory/name) == sha
    write_json(directory/'VERIFICATION.json',dict(status='passed',
        original_historical_agent_margins=True,original_overlap_counts=True,
        independent_sql_repository_and_commit_unions=True,exact_query_total=True,
        file_hashes=True,verification_code_sha256=digest(Path(__file__))))
    print('Paper-table hashes, historical margins and independent queryable repository unions passed.')


if __name__ == '__main__':
    main()

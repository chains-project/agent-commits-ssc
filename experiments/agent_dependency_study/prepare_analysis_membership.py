"""Export existing complete commit-agent membership and stable repository identity.

Reads a saved collection-membership cache and frozen repository metadata.
Writes a portable supplement only; no episode or label changes.
"""
import argparse
import csv
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE/'src'))
from common import csv_rows, digest, read_db, write_json


def required_commits():
    with gzip.open(BASE/'inputs/commit_times/commit_times.csv.gz', 'rt', encoding='utf-8') as f:
        return {(r['repo'], r['sha']): r['commit_id'] for r in csv.DictReader(f)}


def memberships(path, wanted):
    result = defaultdict(set)
    db = read_db(path)
    for row in db.execute('SELECT repo,sha,agent FROM membership'):
        key = row['repo'], row['sha']
        if key in wanted:
            result[key].add(row['agent'])
    db.close()
    assert set(result) == set(wanted), 'Missing attribution'
    return result


def repositories(path, wanted):
    result = {}
    for row in csv_rows(path):
        if row['repo'] not in wanted:
            continue
        numeric = row['status'] == 'ok' and row['id'].isdigit() and int(row['id']) > 0
        value = ('github_id:'+row['id'], 'numeric_id') if numeric else (
            'github_name:'+row['repo'].casefold(), 'metadata_unavailable_name_fallback')
        assert row['repo'] not in result or result[row['repo']] == value
        result[row['repo']] = value
    for repo in wanted:
        result.setdefault(repo, ('github_name:'+repo.casefold(), 'metadata_absent_name_fallback'))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--membership-cache', type=Path, required=True)
    p.add_argument('--metadata', type=Path, required=True)
    args = p.parse_args()
    output = BASE/'inputs/analysis_membership'
    output.mkdir(exist_ok=False)
    wanted = required_commits()
    agents = memberships(args.membership_cache, wanted)
    repos = repositories(args.metadata, {key[0] for key in wanted})
    target = output/'commits.csv.gz'
    with gzip.open(target, 'wt', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['commit_id', 'repo', 'sha', 'agents', 'repository_key', 'repository_resolution'])
        for (repo, sha), cid in sorted(wanted.items()):
            w.writerow([cid, repo, sha, '|'.join(sorted(agents[repo, sha])), *repos[repo]])
    sources = [args.membership_cache, args.metadata, BASE/'inputs/commit_times/manifest.json']
    write_json(output/'manifest.json', dict(
        source_files=[dict(path=str(x), sha256=digest(x)) for x in sources],
        sha256=digest(target), commits=len(wanted), attribution='all frozen qualifying agents',
        repository_identity='numeric GitHub ID; explicit case-folded name fallback',
        cache_origin='summarize_pending_corpus_counts.py; frozen expanded membership, no channel multiplication'))
    print('Exported complete membership for', len(wanted), 'commits', flush=True)


if __name__ == '__main__':
    main()

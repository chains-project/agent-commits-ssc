"""Recover both commit dates from saved raw collection items, without requests.

Inputs: rebuilt Stage1/2, frozen commit metadata, explicit raw JSONL directories.
Outputs: portable commit_times.csv.gz and its source/coverage manifest.
Sync: current Stage3 consumes this supplement; frozen evidence stays unchanged.
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
import sys
from collections import Counter
from pathlib import Path
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE/'src'))
from common import read_db, unpack, digest, write_json
from commit_dates import normalize_date, add_dates, resolve_dates
from stage3_run import writer


def targets(stage, evidence):
    saved = {r['id']: dict(r) for r in evidence.execute('SELECT * FROM commits')}
    result = {}
    for row in stage.execute('SELECT id,body FROM commits'):
        body = unpack(row['body'])
        item = dict(commit_id=row['id'], repo=body['repo'], sha=body['sha'],
                    author_candidates={}, committer_candidates={}, raw_observations=0)
        add_dates(item, body['author_date'], '', 'stage12_author')
        if row['id'] in saved:
            old = saved[row['id']]
            add_dates(item, old['author'], old['committer'], 'frozen:'+old['time_source'])
        key = (body['repo'].casefold(), body['sha'])
        assert key not in result, key
        result[key] = item
    return result


def scan(path, items):
    counts = Counter()
    # Snapshot compressed bytes so decompression does not depend on a live
    # long-running file handle; the hash binds exactly the bytes parsed.
    blob = path.read_bytes()
    with gzip.open(io.BytesIO(blob), 'rt', encoding='utf-8-sig') as stream:
        for line_no, line in enumerate(stream, 1):
            row = json.loads(line)
            counts['rows'] += 1
            key = (row['repo'].casefold(), row['sha'])
            if key not in items:
                continue
            payload = row['item']
            assert payload['sha'] == row['sha'], (path, line_no)
            assert payload['repository']['full_name'].casefold() == key[0]
            commit = payload['commit']
            source = path.parent.parent.name+'/'+path.parent.name+'/'+path.name
            add_dates(items[key], (commit.get('author') or {}).get('date', ''),
                      (commit.get('committer') or {}).get('date', ''), source+':'+str(line_no))
            items[key]['raw_observations'] += 1
            counts['matched'] += 1
    return dict(path=str(path), bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest(), **counts)


def emit(items, output):
    path = output/'commit_times.csv.gz'
    handle, raw = writer(path)
    fields = ['commit_id','repo','sha','author','committer','metadata_status',
              'author_source','committer_source','raw_observations','author_variants','committer_variants']
    out = csv.DictWriter(handle, fieldnames=fields)
    out.writeheader()
    counts = Counter()
    for item in sorted(items.values(), key=lambda r:r['commit_id']):
        row = resolve_dates(item)
        out.writerow(row)
        counts[row['metadata_status']] += 1
        counts['commits'] += 1
        counts['raw_covered'] += int(row['raw_observations'] > 0)
        counts['with_author'] += int(bool(row['author']))
        counts['with_committer'] += int(bool(row['committer']))
    handle.close()
    raw.close()
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage12', type=Path, default=BASE/'results_replay_v2/stage12.sqlite')
    parser.add_argument('--evidence', type=Path, default=BASE/'inputs/observations.sqlite')
    parser.add_argument('--raw-dir', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, default=BASE/'inputs/commit_times')
    args = parser.parse_args()
    if args.output.exists():
        assert not any(args.output.iterdir()), 'Output must be new or empty after an interrupted preparation'
    args.output.mkdir(parents=True, exist_ok=True)
    items = targets(read_db(args.stage12), read_db(args.evidence))
    paths = sorted({p.resolve() for folder in args.raw_dir for p in folder.glob('*.jsonl.gz')})
    assert paths, 'No raw files'
    sources = []
    for index, path in enumerate(paths, 1):
        sources.append(scan(path, items))
        print('Commit dates:', index, '/', len(paths), path.name, sources[-1]['matched'], flush=True)
    counts = emit(items, args.output)
    manifest = dict(version='canonical_v3', counts=counts, sources=sources,
        stage12_sha256=digest(args.stage12), evidence_sha256=digest(args.evidence),
        output_sha256=digest(args.output/'commit_times.csv.gz'), network=False,
        policy='UTC-normalized dates; conflicting dates are withheld; primary committer, author sensitivity only.')
    write_json(args.output/'manifest.json', manifest)
    print(json.dumps(counts), flush=True)


if __name__ == '__main__':
    main()

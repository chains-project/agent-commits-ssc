"""Offline audit of baseline insufficient evidence and Java context opportunities.

Reads frozen sources only; proposals are diagnostic, never automatic labels.
"""
import argparse
import csv
import gzip
import json
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / 'src'))
from common import csv_rows, digest, read_db, unpack, write_json


def write_csv(path, rows):
    rows = list(rows)
    if not rows:
        return
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def sample_reasons(output):
    counts, samples = Counter(), defaultdict(list)
    rng = random.Random(20261007)
    with gzip.open(BASE / 'results/episodes.csv.gz', 'rt', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if row['label'] != 'insufficient_evidence':
                continue
            key = (row['ecosystem'], row['reason'])
            counts[key] += 1
            slot = rng.randrange(counts[key])
            if len(samples[key]) < 5:
                samples[key].append(row)
            elif slot < 5:
                samples[key][slot] = row
    write_csv(output / 'insufficient_samples.csv', [r for k in sorted(samples) for r in samples[k]])
    write_csv(output / 'insufficient_reasons.csv',
              [dict(ecosystem=k[0], reason=k[1], episodes=v) for k, v in counts.most_common()])


def java_inputs(source):
    unresolved = {r['episode_id']: r for r in csv_rows(source / 'episodes.csv') if r['query_kind'] == 'unresolved'}
    event_ids = defaultdict(set)
    for row in csv_rows(source / 'episode_event_links.csv'):
        if row['episode_id'] in unresolved:
            event_ids[row['event_id']].add(row['episode_id'])
    events = defaultdict(list)
    for row in csv_rows(source / 'events.csv'):
        for eid in event_ids.get(row['event_id'], ()):
            events[eid].append(row)
    commits = {r['commit_id']: r for r in csv_rows(source / 'commits.csv')}
    return unresolved, events, commits


def candidates(event, contexts):
    target, path = event['raw_target'], event['path']
    rows = []
    for row in contexts:
        package, dep = row['package_name'], row['dependency_path']
        if ':' not in package or not dep or row['source_path'] != dep:
            continue
        parent = str(Path(dep).parent).replace('\\', '/')
        if parent != '.' and not path.startswith(parent + '/'):
            continue
        group, artifact = package.split(':', 1)
        if not group or '${' in package or artifact.endswith('bom'):
            continue
        if target.startswith(group + '.'):
            rows.append(row)
    if not rows:
        return []
    depth = max(len(str(Path(r['dependency_path']).parent).split('/')) for r in rows)
    return [r for r in rows if len(str(Path(r['dependency_path']).parent).split('/')) == depth]


def java_audit(source, context, output):
    unresolved, events, commits = java_inputs(source)
    db = sqlite3.connect(context.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    by_commit = defaultdict(list)
    for eid, row in unresolved.items():
        by_commit[row['commit_id']].append(eid)
    counts, targets, examples = Counter(), Counter(), []
    for cid, eids in by_commit.items():
        commit = commits[cid]
        rows = list(db.execute('SELECT * FROM contexts WHERE repo_key=? AND sha=?', (commit['repo'].lower(), commit['sha'].lower())))
        declarations = [r for r in rows if r['source_path'] == r['dependency_path'] and ':' in r['package_name']]
        cache = {}
        for eid in eids:
            linked = events[eid]
            pools = []
            for event in linked:
                if event['event_type'] != 'import':
                    continue
                key = (event['raw_target'], event['path'])
                if key not in cache:
                    cache[key] = candidates(event, declarations)
                pools.append(cache[key])
            names = {r['package_name'] for pool in pools for r in pool}
            category = 'unique_group_prefix_candidate' if len(names) == 1 else 'ambiguous_group_prefix' if names else 'no_group_prefix_candidate'
            if not pools:
                category = 'manifest_only_unresolved'
            counts[category] += 1
            targets.update(set(e['raw_target'] for e in linked))
            if len(names) == 1 or len(examples) < 50:
                examples.append(dict(episode_id=eid, repo=commit['repo'], sha=commit['sha'],
                    category=category, targets='|'.join(sorted({e['raw_target'] for e in linked})),
                    paths='|'.join(sorted({e['path'] for e in linked})), packages='|'.join(sorted(names)),
                    declarations=json.dumps([dict(r) for pool in pools for r in pool], ensure_ascii=False)))
    write_csv(output / 'java_context_candidates.csv', examples)
    write_csv(output / 'java_top_unresolved_targets.csv', [dict(target=k, episodes=v) for k,v in targets.most_common(100)])
    write_json(output / 'java_summary.json', dict(episodes=len(unresolved), categories=counts,
        source=str(source), context=str(context), context_sha256=digest(context),
        caution='Unique group prefix is an unvalidated heuristic, not proof of class ownership.'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if not (args.output / 'insufficient_samples.csv').exists():
        sample_reasons(args.output)
    java_audit(args.source, args.context, args.output)
    print((args.output / 'java_summary.json').read_text())


if __name__ == '__main__':
    main()

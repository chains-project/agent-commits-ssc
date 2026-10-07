"""Check recorded temporal evidence for historical canonical positives, offline.

Source-machine supplement. Does not recalculate or change historical labels.
"""
import argparse
import csv
import gzip
import json
import sys
from collections import Counter
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'src'))
from common import csv_rows, digest, write_json
from stage3_classify import timestamp
from paper_counts import save_csv


def indexed(path):
    return {r['episode_id']:r for r in csv_rows(path)}


def temporal(row, metadata, fresh, semver):
    eid = row['episode_id']
    source, prefix = (semver[eid], 'semver') if eid in semver else (fresh[eid], 'fresh')
    commit = timestamp(source['github_committer_date'])
    assert commit == timestamp(metadata[eid]['github_committer_date'])
    assert metadata[eid]['commit_status'] == '200'
    published = timestamp(source[prefix+'_first_matching_publish_at'])
    seconds = int(source[prefix+'_publish_after_committer_seconds'])
    assert int((published-commit).total_seconds()) == seconds
    assert seconds > 86400 if row['final_label']=='confirmed_hallucination' else 0 < seconds <= 86400
    return commit, published, seconds, prefix+'_recorded_release_time'


def maven(row, hidden, artifacts):
    eid = row['episode_id']
    source = hidden[eid]
    assert source['commit_status'] == '200'
    commit = timestamp(source['github_committer_date'])
    seconds = int(source['publish_after_committer_seconds'])
    heads = json.loads(artifacts[eid]['artifact_head_evidence_json'])
    times = [parsedate_to_datetime(x['last_modified']) for x in heads if x['status']==200 and x.get('last_modified')]
    matched = [x for x in times if int((x-commit).total_seconds()) == seconds]
    assert len(matched) == 1 and seconds > 86400
    return commit, matched[0], seconds, 'http_last_modified_proxy'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--canonical-analysis',type=Path,required=True)
    p.add_argument('--historical-evidence',type=Path,required=True)
    p.add_argument('--output',type=Path,default=BASE/'results_paper_tables')
    args = p.parse_args()
    an, old = args.canonical_analysis, args.historical_evidence
    paths = [an/'v2_native_adjudication.csv',an/'v2_temporal_commit_metadata.csv',
             an/'v2_temporal_official_requery.csv',an/'v2_temporal_npm_semver.csv',
             old/'six_language_hidden_postdate_commit_metadata.csv',old/'six_language_maven_artifact_times.csv']
    labels, meta, fresh, semver, hidden, artifacts = [indexed(x) for x in paths]
    with gzip.open(BASE/'inputs/commit_times/commit_times.csv.gz','rt',encoding='utf-8') as f:
        dates = {(r['repo'],r['sha']):r['committer'] for r in csv.DictReader(f)}
    records, counts = [], Counter()
    for row in labels.values():
        if row['final_label'] not in {'confirmed_hallucination','probable_hallucination'}:
            continue
        basis = row['adjudication_basis']
        value = None
        if basis.startswith('first satisfying public version'):
            value = temporal(row,meta,fresh,semver)
        elif basis.startswith('Maven exact version published'):
            value = maven(row,hidden,artifacts)
        if value:
            assert value[0] == timestamp(dates[row['repo'],row['sha']])
        boundary = 'committer' if value else 'no_release_time_comparison'
        counts[row['final_label'],boundary] += 1
        records.append(dict(episode_id=row['episode_id'],label=row['final_label'],
            adjudication_basis=basis,final_boundary=boundary,
            committer=value[0].isoformat() if value else '',
            publication_or_proxy_time=value[1].isoformat() if value else '',
            delta_seconds=value[2] if value else '',time_evidence=value[3] if value else ''))
    assert len(records) == 704
    assert counts['confirmed_hallucination','committer'] == 56
    assert counts['probable_hallucination','committer'] == 25
    save_csv(args.output/'historical_positive_time_evidence.csv',records)
    save_csv(args.output/'historical_positive_time_counts.csv',[
        dict(label=k[0],final_boundary=k[1],episodes=n) for k,n in sorted(counts.items())])
    write_json(args.output/'HISTORICAL_TIME_PROVENANCE.json',dict(status='passed',
        sources=[dict(path=str(x),sha256=digest(x)) for x in [*paths,Path(__file__),BASE/'inputs/commit_times/commit_times.csv.gz']],
        temporal_committer=81,temporal_author_fallback=0,no_release_time_comparison=623,
        validation='Saved temporal deltas recomputed and boundaries matched to recovered raw committer dates.',
        screening_boundary='author',network=False,labels_changed=False,
        outputs={x.name:digest(x) for x in args.output.glob('historical_positive_time_*.csv')}))
    print(dict(counts),flush=True)


if __name__ == '__main__':
    main()

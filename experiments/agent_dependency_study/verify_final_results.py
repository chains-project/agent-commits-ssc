"""Verify repaired Java links and the exact non-Maven baseline reuse in final outputs."""
import argparse
import csv
import gzip
import json
import sys
from collections import Counter,defaultdict
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'src'),str(BASE)]
from common import digest,read_db,write_json
from merge_final_results import read_rows


def verify_global(directory,java):
    baseline=(r for r in read_rows(BASE/'results/episodes.csv.gz') if r['ecosystem']!='Maven')
    seen,counts=set(),Counter()
    for row in read_rows(directory/'episodes.csv.gz'):
        eid=row['episode_id']
        assert eid not in seen
        seen.add(eid)
        assert int(row['final_queryable'])==int(row['query_kind'] in {'exact','range','name_only'})
        if row['ecosystem']=='Maven':
            assert row==java[eid]
        else:
            old=next(baseline)
            assert all(row[k]==v for k,v in old.items()),eid
        if row['label']=='hallucinated_dependency':
            assert row['subtype'] in {'package_no_match','version_no_match','postdate'}
            assert row['evidence_ids'] and row['final_queryable']=='1'
        if row['label']=='present_at_commit' or row['subtype']=='postdate':
            assert row['evidence_ids'] and row['matching_time']
            assert (int(row['delta_seconds'])<=0) if row['label']=='present_at_commit' else (int(row['delta_seconds'])>=0)
        counts[row['label']]+=1
    assert next(baseline,None) is None
    assert len(seen)==1769653-177735+len(java)
    return dict(rows=len(seen),labels=dict(counts),non_maven_rows_identical=1769653-177735)


def verify_links(java_dir,java):
    inputs=read_db(BASE/'inputs/java_stage2.sqlite')
    old_pairs={(r['eid'],r['event_id']) for r in inputs.execute('SELECT * FROM old_links')}
    new_pairs={(r['episode_id'],r['event_id']) for r in read_rows(java_dir/'episode_event_links.csv.gz')}
    expected={r['id'] for r in inputs.execute('SELECT id FROM events')}
    assert {event for _,event in new_pairs}==expected
    assert {eid for eid,_ in new_pairs}==set(java)
    checked_old,checked_new=set(),set()
    recovered=defaultdict(set)
    transitions=defaultdict(set)
    positive_origins=defaultdict(set)
    for row in read_rows(java_dir/'event_bridge.csv.gz'):
        before=(row['old_episode'],row['event_id'])
        after=(row['new_episode'],row['event_id'])
        assert before in old_pairs and after in new_pairs
        checked_old.add(before); checked_new.add(after)
        transitions[row['old_episode'],row['old_label']].add(row['new_label'])
        if row['new_label']=='hallucinated_dependency':
            positive_origins[row['new_episode']].add(row['old_label'])
        if row['old_kind']=='unresolved' and row['new_kind'] in {'exact','range','name_only'}:
            recovered[row['old_episode']].add(row['new_label'])
    assert checked_old==old_pairs and checked_new==new_pairs
    counts=Counter((old_label,'|'.join(sorted(labels))) for (_,old_label),labels in transitions.items())
    with (java_dir/'old_episode_outcomes.csv').open('w',encoding='utf-8',newline='') as handle:
        out=csv.writer(handle); out.writerow(['old_label','destination_labels','old_episodes'])
        out.writerows((*key,n) for key,n in sorted(counts.items()))
    positive_counts=Counter((java[eid]['subtype'],'|'.join(sorted(labels))) for eid,labels in positive_origins.items())
    assert sum(positive_counts.values())==sum(r['label']=='hallucinated_dependency' for r in java.values())
    with (java_dir/'positive_origins.csv').open('w',encoding='utf-8',newline='') as handle:
        out=csv.writer(handle); out.writerow(['final_subtype','source_labels','final_episodes'])
        out.writerows((*key,n) for key,n in sorted(positive_counts.items()))
    return dict(events=len(expected),old_episode_event_pairs=len(old_pairs),new_episode_event_pairs=len(new_pairs),
        recovered_old_unresolved=len(recovered),recovered_labels=dict(Counter('|'.join(sorted(v)) for v in recovered.values())))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results',type=Path,default=BASE/'results_final')
    parser.add_argument('--java',type=Path,default=BASE/'results_java_full')
    args=parser.parse_args()
    summary=json.loads((args.results/'summary.json').read_text())
    for name,sha in summary['output_sha256'].items():
        assert digest(args.results/name)==sha,name
    java={r['episode_id']:r for r in read_rows(args.java/'episodes.csv.gz')}
    checks=verify_global(args.results,java)
    checks.update(verify_links(args.java,java))
    assert checks['labels']==summary['labels']
    checks.update(status='passed',java_episodes=len(java))
    write_json(args.results/'verification.json',checks)
    print(json.dumps(checks,indent=2))


if __name__=='__main__':
    main()

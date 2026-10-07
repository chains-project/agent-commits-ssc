"""Verify complete commit-date use and compare the two otherwise identical runs."""
import argparse
import csv
import json
import sys
from collections import Counter
from itertools import zip_longest
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'src'),str(BASE)]
from common import digest,write_json
from commit_dates import load_dates,normalize_date
from stage3_classify import timestamp
from stage3_run import write_counts
from merge_final_results import read_rows


def temporal(row, dates):
    assert row['boundary']==dates['committer'],row['episode_id']
    assert row['boundary_source']==('committer' if dates['committer'] else 'missing')
    assert row['author_boundary']==dates['author']
    for prefix,label_key,subtype_key,boundary in [('', 'label','subtype',dates['committer']),
                                               ('author_','author_label','author_subtype',dates['author'])]:
        if row[label_key]=='present_at_commit' or row[subtype_key]=='postdate':
            assert boundary and row[prefix+'delta_seconds']
            delta=int(row[prefix+'delta_seconds'])
            assert (delta<=0) if row[label_key]=='present_at_commit' else delta>0
            if not prefix:
                assert delta==int((timestamp(row['matching_time'])-timestamp(boundary)).total_seconds())


def compare(args, dates):
    transitions,author,dates_count=Counter(),Counter(),Counter()
    changes=[];n=0
    keys=['episode_id','commit_id','repo','sha','ecosystem','package_name','effective_package',
          'query_kind','query_value','final_queryable','baseline_id_present','auxiliary_scope_invalidated']
    for old,new in zip_longest(read_rows(args.previous/'episodes.csv.gz'),read_rows(args.results/'episodes.csv.gz')):
        assert old is not None and new is not None
        assert all(old[k]==new[k] for k in keys),new['episode_id']
        temporal(new,dates[new['commit_id']]);n+=1
        transitions[old['label'],old['subtype'],new['label'],new['subtype']]+=1
        author[new['label'],new['subtype'],new['author_label'],new['author_subtype']]+=1
        dates_count['boundary_changed']+=normalize_date(old['boundary'])!=new['boundary']
        dates_count['label_changed']+=old['label']!=new['label']
        dates_count['author_label_differs']+=new['label']!=new['author_label']
        if old['subtype'] in {'package_no_match','version_no_match'}:
            assert (old['label'],old['subtype'])==(new['label'],new['subtype'])
        if old['label']!=new['label']:
            changes.append({k:new[k] for k in keys[:8]} | dict(
                old_label=old['label'],new_label=new['label'],old_subtype=old['subtype'],new_subtype=new['subtype'],
                old_boundary=old['boundary'],new_boundary=new['boundary'],matching_time=new['matching_time'],
                new_reason=new['reason']))
    return n,transitions,author,dates_count,changes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous',type=Path,required=True)
    parser.add_argument('--results',type=Path,required=True)
    args=parser.parse_args()
    summary=json.loads((args.results/'summary.json').read_text())
    dates,manifest=load_dates(BASE/'inputs/commit_times',summary['stage12']['output_sha256'])
    for name,sha in summary['output_sha256'].items():assert digest(args.results/name)==sha,name
    n,transitions,author,counts,changes=compare(args,dates)
    assert n==summary['episodes']
    write_counts(args.results/'time_update_transitions.csv',['old_label','old_subtype','new_label','new_subtype'],transitions)
    write_counts(args.results/'time_update_author_comparison.csv',['committer_label','committer_subtype','author_label','author_subtype'],author)
    path=args.results/'time_update_changed_episodes.csv'
    if changes:
        with path.open('w',encoding='utf-8',newline='') as handle:
            out=csv.DictWriter(handle,fieldnames=list(changes[0]));out.writeheader();out.writerows(changes)
    relations=Counter()
    for row in dates.values():
        a,c=timestamp(row['author']),timestamp(row['committer'])
        relation='missing' if not a or not c else 'same' if a==c else 'committer_later' if c>a else 'committer_earlier'
        relations[relation]+=1
    receipt=dict(status='passed',episodes=n,commit_date_coverage=manifest['counts'],
        commit_date_relations=dict(relations),counts=dict(counts),
        unchanged_stage12_sha256=summary['stage12']['output_sha256'],
        unchanged_registry_sha256=summary['registry_sha256'],
        previous_sha256=digest(args.previous/'episodes.csv.gz'),current_sha256=digest(args.results/'episodes.csv.gz'))
    write_json(args.results/'time_update_verification.json',receipt)
    print(json.dumps(receipt,indent=2),flush=True)


if __name__=='__main__':main()

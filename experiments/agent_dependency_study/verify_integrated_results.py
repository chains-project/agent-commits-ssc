"""Check rebuilt event closure, episode identities and final label conservation."""
import argparse
import csv
import gzip
import json
import sys
from collections import Counter
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'src'),str(BASE/'scripts/hallucination'),str(BASE)]
from common import read_db,unpack,digest,write_json
from merge_final_results import read_rows
from stage3_run import write_counts
from stage_schema import make_event_id,make_episode_id
from commit_dates import load_dates
from stage3_classify import timestamp


def verify_stage12(stage):
    counts=Counter()
    for row in stage.execute('SELECT c.id,c.body AS commit_body,e.body AS events,l.body AS links FROM commits c JOIN events e ON e.cid=c.id JOIN links l ON l.cid=c.id'):
        commit,events,links=unpack(row['commit_body']),unpack(row['events']),unpack(row['links'])
        event_ids={r['event_id'] for r in events}
        for event in events:
            assert event['event_id']==make_event_id(row['id'],event['event_type'],event['path'],int(event['new_line_number']),int(event['event_ordinal']),event['raw_target'])
        assert len(event_ids)==len(events),row['id']
        assert event_ids=={r['event_id'] for r in links},row['id']
        episode_ids=set()
        for episode in stage.execute('SELECT id,eco,pkg,kind,value,q FROM episodes WHERE cid=?',(row['id'],)):
            episode_ids.add(episode['id'])
            if episode['q']:
                assert episode['id']==make_episode_id(row['id'],episode['eco'],episode['pkg'],episode['kind'],episode['value'])
        assert episode_ids=={r['episode_id'] for r in links},row['id']
        assert len(links)==len({(r['event_id'],r['episode_id']) for r in links}),row['id']
        assert int(commit['import_event_count'])==sum(r['event_type']=='import' for r in events)
        assert int(commit['manifest_event_count'])==sum(r['event_type']=='manifest_addition' for r in events)
        counts.update(commits=1,events=len(events),links=len(links),episodes=len(episode_ids))
    return dict(counts)


def verify_labels(stage,directory,summary):
    expected=iter(stage.execute('SELECT id,eco,pkg,kind,value FROM episodes ORDER BY eco,pkg,kind,value,id'))
    number=0;labels=Counter();queryable=0
    dates=None
    if summary.get('boundary_policy')=='committer_only':
        date_dir=Path(summary['commit_dates_path']).parent
        if not date_dir.is_dir():date_dir=BASE/'inputs/commit_times'
        dates,date_manifest=load_dates(date_dir,summary['stage12']['output_sha256'])
        assert date_manifest['output_sha256']==summary['commit_dates']['output_sha256']
    baseline={r['episode_id']:r['label'] for r in read_rows(BASE/'results/episodes.csv.gz')}
    transitions=Counter()
    for row in read_rows(directory/'episodes.csv.gz'):
        eid=row['episode_id'];source=next(expected)
        assert tuple(source)==(eid,row['ecosystem'],row['package_name'],row['query_kind'],row['query_value']),eid
        number+=1;labels[row['label']]+=1
        q=int(row['query_kind'] in {'exact','range','name_only'})
        assert int(row['final_queryable'])==q;queryable+=q
        if dates is not None:
            date=dates[row['commit_id']]
            assert row['boundary']==date['committer'],eid
            assert row['boundary_source']==('committer' if date['committer'] else 'missing'),eid
            assert row['author_boundary']==date['author'],eid
        if row['label']=='hallucinated_dependency':
            assert row['subtype'] in {'package_no_match','version_no_match','postdate'} and q
            assert row['evidence_ids']
        if row['label']=='present_at_commit' or row['subtype']=='postdate':
            assert row['matching_time'] and row['evidence_ids']
            assert (int(row['delta_seconds'])<=0) if row['label']=='present_at_commit' else (int(row['delta_seconds'])>0)
            assert int(row['delta_seconds'])==int((timestamp(row['matching_time'])-timestamp(row['boundary'])).total_seconds())
        transitions[baseline.pop(eid,'new_episode_identity'),row['label']]+=1
    assert next(expected,None) is None and number==summary['episodes']
    assert dict(labels)==summary['labels'] and queryable==summary['queryable']
    for label in baseline.values():transitions[label,'identity_removed_or_replaced']+=1
    write_counts(directory/'identity_transitions.csv',['baseline_label','current_label'],transitions)
    return dict(rows=number,queryable=queryable,labels=dict(labels),removed_or_replaced_identities=len(baseline),
        added_identities=sum(v for k,v in transitions.items() if k[0]=='new_episode_identity'))


def verify_witnesses(stage12,stage):
    files=events=0
    previous=None;by_id={}
    with gzip.open(stage12/'manifest_witnesses.jsonl.gz','rt',encoding='utf-8') as handle:
        for line in handle:
            row=json.loads(line)
            assert len(row['child_sha256'])==64 and row['child_sha'] and row['path']
            if row['commit_id']!=previous:
                previous=row['commit_id']
                saved=stage.execute('SELECT body FROM events WHERE cid=?',(previous,)).fetchone()
                by_id={r['event_id']:r for r in unpack(saved['body'])} if saved else {}
            for eid in row['new_events']:
                assert eid in by_id and by_id[eid]['path']==row['path'],eid
                assert int(by_id[eid]['new_line_number']) in row['added_lines'],eid
            files+=1;events+=len(row['new_events'])
    return dict(snapshot_files=files,snapshot_manifest_events=events)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage12',type=Path,default=BASE/'results_replay_v2')
    parser.add_argument('--results',type=Path,default=BASE/'results_integrated')
    args=parser.parse_args();summary=json.loads((args.results/'summary.json').read_text())
    for name,sha in summary['output_sha256'].items():assert digest(args.results/name)==sha,name
    stage=read_db(args.stage12/'stage12.sqlite')
    checks=dict(stage12=verify_stage12(stage),classification=verify_labels(stage,args.results,summary),
        witnesses=verify_witnesses(args.stage12,stage),status='passed')
    for key,value in checks['stage12'].items():assert value==summary['stage12']['counts'][key],key
    write_json(args.results/'verification.json',checks);print(json.dumps(checks,indent=2))


if __name__=='__main__':main()

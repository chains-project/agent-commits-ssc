"""Count source commit keys and retained Stage1 events outside the snapshot index."""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'src'))
from common import read_db,unpack,write_json


def summarize(db):
    counts=Counter()
    for row in db.execute('SELECT e.body FROM events e LEFT JOIN patches p ON p.cid=e.cid WHERE p.cid IS NULL'):
        events=unpack(row['body']);number=sum(r['event_type']=='manifest_addition' for r in events)
        counts['commits_with_old_manifest_events' if number else 'commits_without_old_manifest_events']+=1
        counts['old_manifest_events']+=number
    queries={
        'previous_event_commits':'SELECT count(*) FROM commits',
        'snapshot_linked_patch_commits':'SELECT count(*) FROM patches',
        'overlap':'SELECT count(*) FROM patches p JOIN commits c ON c.id=p.cid',
        'additional_patch_commits':'SELECT count(*) FROM patches p LEFT JOIN commits c ON c.id=p.cid WHERE c.id IS NULL',
        'old_commits_without_snapshot_patch_index':'SELECT count(*) FROM commits c LEFT JOIN patches p ON p.cid=c.id WHERE p.cid IS NULL'}
    counts.update({k:db.execute(q).fetchone()[0] for k,q in queries.items()})
    counts['total_candidate_keys']=counts['previous_event_commits']+counts['additional_patch_commits']
    assert counts['commits_with_old_manifest_events']+counts['commits_without_old_manifest_events']==counts['old_commits_without_snapshot_patch_index']
    return dict(counts)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=BASE/'results_remaining_review/no_snapshot_commit_coverage.json')
    args=parser.parse_args();result=summarize(read_db(BASE/'inputs/replay.sqlite'))
    result['source_sha256']=json.loads((BASE/'inputs/replay.manifest.json').read_text())['sha256']
    write_json(args.output,result);print(json.dumps(result,indent=2))


if __name__=='__main__':main()

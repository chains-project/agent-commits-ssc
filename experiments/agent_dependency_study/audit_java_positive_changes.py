"""Trace newly positive Java identities to declarations and saved registry scope."""
import json
import gzip
import sys
from collections import defaultdict
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'src'),str(BASE)]
from common import read_db,unpack,write_json
from merge_final_results import read_rows


def main():
    directory=BASE/'results_java_full'
    origins=defaultdict(set)
    for row in read_rows(directory/'event_bridge.csv.gz'):
        if row['new_label']=='hallucinated_dependency':
            origins[row['new_episode']].add(row['old_label'])
    selected={r['episode_id']:dict(result=r,mapping_witnesses=[]) for r in read_rows(directory/'episodes.csv.gz')
              if r['label']=='hallucinated_dependency' and 'hallucinated_dependency' not in origins[r['episode_id']]}
    with gzip.open(directory/'mapping_witnesses.jsonl.gz','rt',encoding='utf-8') as handle:
        for line in handle:
            witness=json.loads(line)
            for eid in set(witness['episode_ids']) & selected.keys():
                selected[eid]['mapping_witnesses'].append(witness)
    db=read_db(BASE/'inputs/observations.sqlite')
    for value in selected.values():
        row=value['result']
        records=[unpack(db.execute('SELECT body FROM observations WHERE id=?',(oid,)).fetchone()['body'])
                 for oid in row['evidence_ids'].split('|')]
        assert value['mapping_witnesses']
        assert row['subtype']=='version_no_match'
        assert all(r['scope']=='enumeration' and r['lookup_status']=='ok' and r['versions'] for r in records)
        assert all(row['query_value'] not in {v['version'] for v in r['versions']} for r in records)
        value['registry_summary']=[dict(scope=r['scope'],lookup_status=r['lookup_status'],
            versions=len(r['versions']),origin=r.get('origin')) for r in records]
    write_json(directory/'new_positive_evidence.json',dict(status='passed',episodes=len(selected),cases=selected))
    print('New Java positive evidence checked:',len(selected))


if __name__=='__main__':
    main()

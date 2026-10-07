"""Check the Java pilot against frozen baseline labels and event-level partitions."""
import argparse
import csv
import gzip
import sys
from collections import Counter,defaultdict
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'src'))
from common import csv_rows,digest,write_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results',type=Path,required=True)
    args=parser.parse_args()
    old={r['episode_id']:r for r in csv_rows(args.results/'old_classification.csv')}
    new={r['episode_id']:r for r in csv_rows(args.results/'new_classification.csv')}
    checked=set()
    with gzip.open(BASE/'results/episodes.csv.gz','rt',encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if row['episode_id'] not in old:
                continue
            reference=old[row['episode_id']]
            for key in ['label','boundary','matching_version','delta_seconds']:
                assert reference[key]==row[key],(row['episode_id'],key)
            assert reference['final_reason']==row['reason']
            checked.add(row['episode_id'])
    assert checked==set(old)
    links=list(csv_rows(args.results/'new_episode_event_links.csv'))
    assert len({(r['episode_id'],r['event_id']) for r in links})==len(links)
    expected_events={r['event_id'] for r in csv_rows(BASE/'samples/java_identity/events.csv')}
    assert {r['event_id'] for r in links}==expected_events
    assert {r['episode_id'] for r in links}==set(new)
    recovered=defaultdict(set)
    for row in csv_rows(args.results/'event_bridge.csv'):
        if row['old_kind']=='unresolved' and row['new_kind'] in {'exact','range','name_only'}:
            recovered[row['old_episode']].add(row['new_episode'])
    destinations={eid for eids in recovered.values() for eid in eids}
    codes=['scripts/hallucination/stage2_java_identity.py','scripts/hallucination/stage2_episode_linker.py',
           'scripts/hallucination/java_class_catalog.json','run_java_pilot.py']
    output=dict(status='passed',frozen_baseline_rows_verified=len(checked),event_closure=len(expected_events),
        recovered_old_ids=len(recovered),new_destination_ids=len(destinations),
        destinations_already_in_baseline=len(destinations & set(old)),
        recovered_outcomes=dict(Counter('|'.join(sorted({new[eid]['label'] for eid in eids})) for eids in recovered.values())),
        source_sha256={name:digest(BASE/name) for name in codes},
        result_sha256={p.name:digest(p) for p in sorted(args.results.iterdir()) if p.is_file() and p.name!='verification.json'})
    write_json(args.results/'verification.json',output)
    print(output)


if __name__=='__main__':
    main()

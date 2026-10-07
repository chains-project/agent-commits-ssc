"""Independently check output partitions and rule invariants, without reclassification."""
import argparse
import csv
import gzip
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'src'))
from common import digest,read_db,write_json

LABELS={'present_at_commit','hallucinated_dependency','insufficient_evidence','out_of_scope'}
SUBTYPES={'package_no_match','version_no_match','postdate'}


def check_row(row):
    assert row['label'] in LABELS
    assert (row['subtype'] in SUBTYPES) == (row['label']=='hallucinated_dependency')
    if row['label']!='hallucinated_dependency':
        assert row['subtype']==''
    if row['label']=='present_at_commit' or row['subtype']=='postdate':
        delta=int(row['delta_seconds'])
        before=datetime.fromisoformat(row['matching_time'])<=datetime.fromisoformat(row['boundary'].replace('Z','+00:00'))
        assert before == (row['label']=='present_at_commit')
        assert delta<=0 if before else delta>=0
        assert row['evidence_ids']
    if row['label']=='hallucinated_dependency':
        assert row['baseline_queryable']=='1'
        assert row['evidence_ids']


def scan(path,db):
    expected=iter(db.execute('SELECT id,q FROM episodes ORDER BY eco,pkg,kind,value,id'))
    counts,labels,ecosystems,examples=Counter(),Counter(),Counter(),{}
    timing=Counter()
    with gzip.open(path,'rt',encoding='utf-8',newline='') as handle:
        for row in csv.DictReader(handle):
            identity=next(expected)
            assert row['episode_id']==identity['id']
            assert int(row['baseline_queryable'])==identity['q']
            check_row(row)
            key=(row['ecosystem'],row['label'],row['subtype'])
            examples.setdefault(key,row)
            counts['episodes']+=1
            counts['baseline_queryable']+=int(row['baseline_queryable'])
            labels[row['label']]+=1
            ecosystems[row['ecosystem']]+=1
            if row['label']=='present_at_commit' or row['subtype']=='postdate':
                timing[row['label']+'|'+row['boundary_source']]+=1
    assert next(expected,None) is None
    return dict(counts=counts,labels=labels,ecosystems=ecosystems,time_comparisons=timing),examples


def crosscheck(directory,observed):
    summary=json.loads((directory/'summary.json').read_text())
    assert summary['episodes']==observed['counts']['episodes']==1769653
    assert observed['counts']['baseline_queryable']==1364856
    assert summary['labels']==observed['labels']
    for name,sha in summary['output_sha256'].items():
        assert digest(directory/name)==sha,name
    for name in ['reasons.csv','transitions.csv','proxy_sensitivity.csv','coverage.csv']:
        with (directory/name).open(encoding='utf-8') as handle:
            assert sum(int(r['episodes']) for r in csv.DictReader(handle))==1769653,name
    with (directory/'label_counts.csv').open(encoding='utf-8') as handle:
        rows=list(csv.DictReader(handle))
    assert sum(int(r['episodes']) for r in rows if r['stratum']=='global')==1769653
    assert sum(int(r['episodes']) for r in rows if r['stratum']!='global')==1769653


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results',type=Path,default=BASE/'results')
    args=parser.parse_args()
    db=read_db(BASE/'inputs/observations.sqlite')
    observed,examples=scan(args.results/'episodes.csv.gz',db)
    crosscheck(args.results,observed)
    observed['checks']='ordered identity and baseline equality; label/subtype/time/evidence invariants; global/ecosystem/summary closure; output hashes'
    observed['status']='passed'
    write_json(args.results/'verification.json',observed)
    with (args.results/'examples.csv').open('w',encoding='utf-8',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(next(iter(examples.values()))))
        writer.writeheader()
        writer.writerows(examples[k] for k in sorted(examples))
    print(json.dumps(observed,indent=2))


if __name__=='__main__':
    main()

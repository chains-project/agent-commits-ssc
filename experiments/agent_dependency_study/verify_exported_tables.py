"""Confirm that exported structural episodes and current final labels match exactly."""
import argparse
import csv
import gzip
import json
import sys
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'src'))
from common import digest,write_json


def ids(path):
    with gzip.open(path,'rt',encoding='utf-8',newline='') as handle:
        for row in csv.DictReader(handle):yield row['episode_id']


def verify(tables,results):
    summary=json.loads((results/'summary.json').read_text())
    manifest=json.loads((tables/'manifest.json').read_text())
    assert manifest['source_stage12_sha256']==summary['stage12']['output_sha256']
    final=results/'episodes.csv.gz';export=tables/'episodes.csv.gz'
    assert digest(final)==summary['output_sha256']['episodes.csv.gz']
    record=next(r for r in manifest['tables'] if r['path']=='episodes.csv.gz')
    assert digest(export)==record['sha256']
    remaining=set(ids(final));assert len(remaining)==summary['episodes'];count=0
    for eid in ids(export):remaining.remove(eid);count+=1
    assert not remaining and count==summary['episodes']==record['rows']
    result=dict(status='passed',episodes=count,queryable=summary['queryable'],
        source_stage12_sha256=manifest['source_stage12_sha256'],
        structural_episodes_sha256=record['sha256'],final_labels_sha256=summary['output_sha256']['episodes.csv.gz'],
        check='Exact episode-ID set equality; no duplicate or missing exported IDs; source and both output hashes match the current integrated run.')
    write_json(tables/'final_label_join_verification.json',result);print(json.dumps(result))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tables',type=Path,required=True)
    parser.add_argument('--results',type=Path,default=BASE/'results_integrated')
    args=parser.parse_args();verify(args.tables,args.results)

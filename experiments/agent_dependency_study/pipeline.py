"""Offline Stage1/2 and general staged classification, plus one real-commit demo."""
import argparse
import csv
import json
import sys
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.dont_write_bytecode=True
for folder in ['vendor','src','scripts/hallucination']:
    sys.path.insert(0,str(BASE/folder))
from common import csv_rows,write_json,dump
from stage3_classify import classify
from stage3_matching import Matcher
from stage3_prepare_base import time_type


def extract(index,population,output,languages=()):
    from stage1_extract import Stage1Config,run_stage1
    return run_stage1(Stage1Config(index,population,output,actual_languages=tuple(languages),
                                  scan_all_repo_languages=True,sample_per_repo_language=0))


def align(directory,context_db=None,context_rows=None):
    from stage2_build import run_stage2
    from stage2_context import MemoryContextLookup
    lookup=MemoryContextLookup(context_rows) if context_rows is not None else None
    return run_stage2(directory,context_db=context_db,context_lookup=lookup)


def staged_classification(directory,evidence_path,output,node='node',commit_times=None):
    import hashlib
    observations={}
    with evidence_path.open(encoding='utf-8-sig') as handle:
        for line in handle:
            row=json.loads(line)
            if row['evidence_type']!='registry':
                continue
            row.update(time_type=time_type(row['ecosystem'],row['source']),negative_kind='package',
                       scope='observed_subset' if row.get('reuse_scope') else 'enumeration')
            oid=hashlib.sha256(dump(row).encode()).hexdigest()
            observations.setdefault((row['ecosystem'],row['package_name']),[]).append((oid,row))
    from commit_dates import load_dates,normalize_date
    commit_times=commit_times or BASE/'inputs/commit_times'
    dates,_=load_dates(commit_times)
    boundaries={row['commit_id']:dates.get(row['commit_id'],{}).get('committer',
        normalize_date(row.get('committer_date',''))) for row in csv_rows(directory/'commits.csv')}
    matcher=Matcher(node)
    count=0
    with output.open('x',encoding='utf-8') as handle:
        for row in csv_rows(directory/'episodes.csv'):
            records=observations.get((row['ecosystem'],row['package_name']),[])
            boundary=boundaries[row['commit_id']]
            result=classify(row,records,boundary,matcher)
            handle.write(dump(dict(episode_id=row['episode_id'],boundary=boundary,
                boundary_source='committer' if boundary else 'missing',**result))+'\n')
            count+=1
    matcher.close()
    return count


def demo(output,node):
    output.mkdir(parents=True,exist_ok=False)
    sample=BASE/'sample'
    index=list(csv_rows(sample/'commit_index.csv'))
    for row in index:
        row['patch_path']=str(sample/'commit.patch')
    with (output/'index.csv').open('w',encoding='utf-8',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(index[0]))
        writer.writeheader()
        writer.writerows(index)
    tables=output/'tables'
    extract(output/'index.csv',sample/'population.csv',tables,['TypeScript'])
    align(tables,context_rows=json.loads((sample/'context_rows.json').read_text()))
    from stage3_apply import run_stage3
    run_stage3(tables,sample/'stage3_evidence.jsonl')
    n=staged_classification(tables,sample/'stage3_evidence.jsonl',output/'new_labels.jsonl',node)
    write_json(output/'demo_summary.json',dict(episodes=n,repo=index[0]['repo'],sha=index[0]['sha'],
        scope='Current bundled extraction/alignment, saved registry evidence, new classifier; not a canonical full rerun.'))
    print((output/'new_labels.jsonl').read_text(),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest='command',required=True)
    p=subs.add_parser('demo'); p.add_argument('--output',type=Path,required=True); p.add_argument('--node',default='node')
    p=subs.add_parser('stage1')
    for arg in ['index','population','output']:
        p.add_argument('--'+arg,type=Path,required=True)
    p.add_argument('--languages',nargs='*',default=[])
    p=subs.add_parser('stage2'); p.add_argument('--input-dir',type=Path,required=True); p.add_argument('--context-db',type=Path)
    p=subs.add_parser('classify-staged')
    for arg in ['input-dir','registry-jsonl','output']:
        p.add_argument('--'+arg,type=Path,required=True)
    p.add_argument('--node',default='node')
    p.add_argument('--commit-times',type=Path,default=BASE/'inputs/commit_times')
    args=parser.parse_args()
    if args.command=='demo': demo(args.output,args.node)
    elif args.command=='stage1': extract(args.index,args.population,args.output,args.languages)
    elif args.command=='stage2': align(args.input_dir,args.context_db)
    else: staged_classification(args.input_dir,args.registry_jsonl,args.output,args.node,args.commit_times)


if __name__=='__main__':
    main()

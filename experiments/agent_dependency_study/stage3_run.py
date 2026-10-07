"""Offline full-population unified classification. Requires Python and Node only."""
import argparse
import csv
import gzip
import io
import json
import sys
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE/'src'))
from common import read_db,unpack,digest,write_json,dump
from stage3_classify import classify,collect_facts
from stage3_matching import Matcher


def write_counts(path,fields,counts):
    with path.open('w',encoding='utf-8',newline='') as handle:
        writer=csv.writer(handle)
        writer.writerow([*fields,'episodes'])
        writer.writerows([*key,n] for key,n in sorted(counts.items()))


def writer(path):
    raw=path.open('wb')
    gz=gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0)
    return io.TextIOWrapper(gz,encoding='utf-8',newline=''),raw


def execute(db,output,node,limit=0):
    matcher=Matcher(node)
    @lru_cache(maxsize=8192)
    def observations(eco,pkg):
        return [(r['id'],unpack(r['body'])) for r in db.execute('SELECT id,body FROM observations WHERE eco=? AND pkg=? ORDER BY id',(eco,pkg))]
    @lru_cache(maxsize=2048)
    def facts(eco,pkg,kind,value,exclude):
        episode=dict(ecosystem=eco,query_kind=kind,query_value=value)
        return collect_facts(episode,observations(eco,pkg),matcher,exclude)
    counts,transitions,reasons,sensitivity=Counter(),Counter(),Counter(),Counter()
    proxy_changes,coverage=Counter(),Counter()
    fields=['episode_id','repo','sha','ecosystem','package_name','effective_package','query_kind','query_value','baseline_queryable','label','subtype','reason','boundary','boundary_source','matching_version','matching_time','delta_seconds','time_type','proxy_time','proxy_excluded_label','proxy_excluded_subtype','conflict_observed','evidence_ids','auxiliary_ref','upstream_reasons','historical_advisory_flag']
    handle,raw=writer(output/'episodes.csv.gz')
    csvout=csv.DictWriter(handle,fieldnames=fields,extrasaction='ignore')
    csvout.writeheader()
    query='SELECT e.*,c.repo,c.sha,c.author,c.committer,c.time_source,a.body AS aux,l.label AS old FROM episodes e JOIN commits c ON c.id=e.cid LEFT JOIN auxiliary a ON a.eid=e.id LEFT JOIN old_labels l ON l.eid=e.id ORDER BY e.eco,e.pkg,e.kind,e.value,e.id'
    n=0
    for row in db.execute(query):
        if limit and n>=limit:
            break
        record=evaluate_row(row,observations,matcher,facts)
        csvout.writerow(record)
        tally(row,record,counts,reasons,transitions)
        proxy_changes[(row['eco'],str(row['q']),record['label'],record['proxy_excluded_label'],record['proxy_excluded_subtype'])]+=1
        coverage[(record['boundary_source'],str(bool(record['auxiliary_ref'])),str(record.get('conflict_observed',False)))]+=1
        # All proxy positives and all close postdates are explicitly separable.
        if record['label']=='hallucinated_dependency':
            sensitivity[(record['subtype'],str(record['proxy_time']),str(record['subtype']=='postdate' and int(record['delta_seconds'])<=86400))]+=1
        n+=1
        if n%100000==0:
            print('Classified',n,flush=True)
    handle.close()
    raw.close()
    matcher.close()
    write_counts(output/'label_counts.csv',['stratum','baseline_queryable','label','subtype'],counts)
    write_counts(output/'reasons.csv',['ecosystem','label','reason'],reasons)
    write_counts(output/'transitions.csv',['old_label','new_label','subtype'],transitions)
    write_counts(output/'positive_sensitivity.csv',['subtype','proxy_time','within_24h'],sensitivity)
    write_counts(output/'proxy_sensitivity.csv',['ecosystem','baseline_queryable','main_label','without_proxy_label','without_proxy_subtype'],proxy_changes)
    write_counts(output/'coverage.csv',['boundary_source','episode_corrective_context','conflict_observed'],coverage)
    return n,counts


def evaluate_row(row,observations,matcher,facts,boundary_mode='legacy'):
    episode=unpack(row['body'])
    aux=unpack(row['aux']) if row['aux'] else {}
    target=aux.get('effective_package',row['pkg'])
    boundary,origin=select_boundary(row,boundary_mode)
    records=observations(row['eco'],target)
    key=(row['eco'],target,row['kind'],row['value'])
    value=classify(episode,records,boundary,matcher,aux,prepared_facts=facts(*key,False) if row['q'] else None)
    without=classify(episode,records,boundary,matcher,aux,exclude_proxies=True,
                     prepared_facts=facts(*key,True) if row['q'] else None)
    value.update(episode_id=row['id'],repo=row['repo'],sha=row['sha'],ecosystem=row['eco'],
        package_name=row['pkg'],effective_package=target,query_kind=row['kind'],query_value=row['value'],
        baseline_queryable=row['q'],boundary=boundary,boundary_source=origin,
        evidence_ids='|'.join(value['evidence_ids']),auxiliary_ref=row['id'] if aux else '',
        proxy_excluded_label=without['label'],proxy_excluded_subtype=without['subtype'],
        upstream_reasons=episode['reason_codes'],historical_advisory_flag='historical_advisory_precedes_current_absence' in episode['reason_codes'])
    return value


def select_boundary(row,mode):
    if mode in {'committer','author'}:
        return row[mode],mode if row[mode] else 'missing'
    assert mode=='legacy',mode
    return (row['committer'] or row['author'],
            'committer' if row['committer'] else 'author_fallback' if row['author'] else 'missing')


def tally(row,value,counts,reasons,transitions):
    for stratum in ['global',row['eco']]:
        counts[(stratum,str(row['q']),value['label'],value['subtype'])]+=1
    reasons[(row['eco'],value['label'],value['reason'])]+=1
    transitions[(row['old'] or 'not_in_candidate_review',value['label'],value['subtype'])]+=1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=BASE/'inputs/observations.sqlite')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--node',default='node')
    parser.add_argument('--limit',type=int,default=0)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    start=time.monotonic()
    db=read_db(args.input)
    manifest=json.loads((args.input.parent/'manifest.json').read_text(encoding='utf-8'))
    assert digest(args.input)==manifest['input_sha256'],'Input fingerprint mismatch'
    n,counts=execute(db,args.output,args.node,args.limit)
    if not args.limit:
        assert n==1769653
        assert sum(v for k,v in counts.items() if k[0]=='global' and k[1]=='1')==1364856
    summary=dict(episodes=n,elapsed_seconds=time.monotonic()-start,input_sha256=digest(args.input),policy_sha256=digest(BASE/'policy.json'),labels={})
    for key,value in counts.items():
        if key[0]=='global':
            summary['labels'][key[2]]=summary['labels'].get(key[2],0)+value
    summary['output_sha256']={p.name:digest(p) for p in args.output.iterdir() if p.is_file()}
    write_json(args.output/'summary.json',summary)
    print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__':
    main()

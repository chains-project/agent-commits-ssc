"""Replace the complete baseline Maven slice and summarize final global results."""
import argparse
import csv
import gzip
import json
import sys
from collections import Counter
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'src'),str(BASE)]
from common import digest,write_json
from stage3_run import writer,write_counts


def read_rows(path):
    with gzip.open(path,'rt',encoding='utf-8',newline='') as handle:
        yield from csv.DictReader(handle)


def merged_rows(java):
    injected=False
    key=lambda r:(r['effective_package'],r['query_kind'],r['query_value'],r['episode_id'])
    for row in read_rows(BASE/'results/episodes.csv.gz'):
        if row['ecosystem']=='Maven':
            if not injected:
                yield from sorted(java,key=key)
                injected=True
            continue
        row.update(final_queryable=row['baseline_queryable'],baseline_id_present='True',languages='')
        yield row
    assert injected


def tally(row,counts,reasons,proxies,timing,positive):
    for stratum in ['global',row['ecosystem']]:
        counts[stratum,row['final_queryable'],row['label'],row['subtype']]+=1
    reasons[row['ecosystem'],row['label'],row['reason']]+=1
    proxies[row['ecosystem'],row['proxy_excluded_label'],row['proxy_excluded_subtype']]+=1
    if row['label']=='present_at_commit' or row['subtype']=='postdate':
        timing[row['label'],row['boundary_source']]+=1
    if row['label']=='hallucinated_dependency':
        positive[row['subtype'],row['proxy_time'],str(row['subtype']=='postdate' and int(row['delta_seconds'])<=86400)]+=1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--java',type=Path,default=BASE/'results_java_full')
    parser.add_argument('--output',type=Path,default=BASE/'results_final')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    java_summary=json.loads((args.java/'summary.json').read_text())
    java=list(read_rows(args.java/'episodes.csv.gz'))
    assert len(java)==java_summary['episodes']
    fields=list(java[0])
    handle,raw=writer(args.output/'episodes.csv.gz')
    out=csv.DictWriter(handle,fieldnames=fields,extrasaction='ignore'); out.writeheader()
    counts,reasons,proxies,timing,positive=Counter(),Counter(),Counter(),Counter(),Counter()
    identities=set()
    for row in merged_rows(java):
        assert row['episode_id'] not in identities,row['episode_id']
        identities.add(row['episode_id'])
        out.writerow(row)
        tally(row,counts,reasons,proxies,timing,positive)
    handle.close(); raw.close()
    assert len(identities)==1769653-177735+len(java)
    write_counts(args.output/'label_counts.csv',['stratum','queryable','label','subtype'],counts)
    write_counts(args.output/'reasons.csv',['ecosystem','label','reason'],reasons)
    write_counts(args.output/'proxy_sensitivity.csv',['ecosystem','label','subtype'],proxies)
    write_counts(args.output/'time_boundaries.csv',['label','boundary_source'],timing)
    write_counts(args.output/'positive_sensitivity.csv',['subtype','proxy_time','within_24h'],positive)
    global_counts={label:sum(v for k,v in counts.items() if k[0]=='global' and k[2]==label) for label in {k[2] for k in counts}}
    summary=dict(episodes=len(identities),queryable=sum(v for k,v in counts.items() if k[0]=='global' and k[1]=='1'),
        labels=global_counts,baseline_episodes=1769653,baseline_queryable=1364856,
        java=java_summary,method='Entire baseline Maven slice replaced by repaired Stage2/Stage3 Java outputs; other ecosystems reused unchanged.',
        baseline_summary_sha256=digest(BASE/'results/summary.json'),java_summary_sha256=digest(args.java/'summary.json'))
    summary['output_sha256']={p.name:digest(p) for p in args.output.iterdir() if p.is_file()}
    write_json(args.output/'summary.json',summary)
    print(json.dumps({k:v for k,v in summary.items() if k not in {'java','output_sha256'}},indent=2))


if __name__=='__main__':
    main()

"""Classify every rebuilt episode using existing, frozen registry observations."""
import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'src'),str(BASE)]
from common import read_db,digest,write_json
from replay_classification import Evaluation,commit_metadata
from stage3_run import writer,write_counts
from merge_final_results import tally
from commit_dates import load_dates

FIELDS=['episode_id','commit_id','repo','sha','ecosystem','package_name','effective_package','query_kind','query_value',
    'baseline_queryable','final_queryable','baseline_id_present','label','subtype','reason','boundary','boundary_source',
    'commit_time_provenance','matching_version','matching_time','delta_seconds','time_type','proxy_time',
    'proxy_excluded_label','proxy_excluded_subtype','conflict_observed','evidence_ids','auxiliary_ref',
    'auxiliary_scope_invalidated','upstream_reasons','historical_advisory_flag',
    'author_boundary','author_label','author_subtype','author_reason','author_delta_seconds']
QUERY='''SELECT e.*,c.events_changed,a.body AS aux,o.body AS old_body,o.q AS old_q
FROM episodes e JOIN commits c ON c.id=e.cid
LEFT JOIN frozen.episodes o ON o.id=e.id LEFT JOIN frozen.auxiliary a ON a.eid=e.id
ORDER BY e.eco,e.pkg,e.kind,e.value,e.id'''


def execute(stage,evidence,output,node,date_rows):
    evaluator=Evaluation(evidence,commit_metadata(stage,evidence,date_rows),node,stage)
    stage.execute('ATTACH DATABASE ? AS frozen',(BASE.joinpath('inputs/observations.sqlite').resolve().as_uri()+'?mode=ro',))
    handle,raw=writer(output/'episodes.csv.gz')
    out=csv.DictWriter(handle,fieldnames=FIELDS,extrasaction='ignore');out.writeheader()
    counts,reasons,proxies,timing,positive=Counter(),Counter(),Counter(),Counter(),Counter()
    sensitivity=Counter()
    for number,row in enumerate(stage.execute(QUERY),1):
        value=evaluator.evaluate(row);out.writerow(value)
        tally(value,counts,reasons,proxies,timing,positive)
        sensitivity[(value['ecosystem'],value['final_queryable'],value['label'],value['subtype'],value['author_label'],value['author_subtype'])]+=1
        if number%100000==0:print('Stage3 classified',number,flush=True)
    handle.close();raw.close();evaluator.matcher.close()
    write_counts(output/'label_counts.csv',['stratum','queryable','label','subtype'],counts)
    write_counts(output/'reasons.csv',['ecosystem','label','reason'],reasons)
    write_counts(output/'proxy_sensitivity.csv',['ecosystem','label','subtype'],proxies)
    write_counts(output/'time_boundaries.csv',['label','boundary_source'],timing)
    write_counts(output/'positive_sensitivity.csv',['subtype','proxy_time','within_24h'],positive)
    write_counts(output/'author_sensitivity.csv',
        ['ecosystem','queryable','committer_label','committer_subtype','author_label','author_subtype'],sensitivity)
    return dict(episodes=number,queryable=sum(v for k,v in counts.items() if k[0]=='global' and k[1]=='1'),
        labels={label:sum(v for k,v in counts.items() if k[0]=='global' and k[2]==label) for label in {k[2] for k in counts}})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage12',type=Path,default=BASE/'results_replay_v2')
    parser.add_argument('--output',type=Path,default=BASE/'results_integrated')
    parser.add_argument('--commit-times',type=Path,default=BASE/'inputs/commit_times')
    parser.add_argument('--node',default='node');args=parser.parse_args()
    args.output.mkdir(exist_ok=False);start=time.monotonic()
    stage12=json.loads((args.stage12/'stage12_summary.json').read_text())
    assert digest(args.stage12/'stage12.sqlite')==stage12['output_sha256']
    evidence_manifest=json.loads((BASE/'inputs/manifest.json').read_text())
    assert digest(BASE/'inputs/observations.sqlite')==evidence_manifest['input_sha256']
    date_rows,date_manifest=load_dates(args.commit_times,stage12['output_sha256'])
    summary=execute(read_db(args.stage12/'stage12.sqlite'),read_db(BASE/'inputs/observations.sqlite'),args.output,args.node,date_rows)
    assert summary['episodes']==stage12['counts']['episodes']
    assert summary['queryable']==stage12['counts']['queryable']
    summary.update(stage12=stage12,run_date='2026-10-07',method='Verified child-snapshot manifest replay, complete global Stage2 rebuilding and uniform saved-evidence classification.',
        elapsed_seconds=time.monotonic()-start,stage12_path=str(args.stage12),
        registry_sha256=evidence_manifest['input_sha256'],policy_sha256=digest(BASE/'policy.json'),
        catalog_sha256=digest(BASE/'scripts/hallucination/java_class_catalog.json'))
    summary.update(result_version='canonical_v3',boundary_policy='committer_only',
        commit_dates=date_manifest,commit_dates_path=str(args.commit_times/'commit_times.csv.gz'),
        author_sensitivity='Same episodes and registry evidence; author date is a separate comparison, never a fallback.')
    summary['output_sha256']={p.name:digest(p) for p in args.output.iterdir() if p.is_file()}
    write_json(args.output/'summary.json',summary);print(json.dumps(summary['labels']),flush=True)


if __name__=='__main__':main()

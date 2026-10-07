"""Read-only inventory of remaining gaps in the final repaired population."""
import csv
import json
import sys
from collections import Counter,defaultdict
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'src'),str(BASE)]
from common import read_db,unpack,write_json
from merge_final_results import read_rows
from research_java_catalog import PROJECTS,source_index,match_target


def save(path,fields,counts):
    with path.open('w',encoding='utf-8',newline='') as handle:
        out=csv.writer(handle); out.writerow([*fields,'episodes'])
        out.writerows((*key,n) for key,n in counts.most_common())


def cached_java_candidates(output):
    indices=[]
    for repo,ref,group,modules in PROJECTS:
        path=BASE/'research/java_high_frequency/trees'/(repo.replace('/','__')+'.json')
        tree=json.loads(path.read_text())
        assert not tree.get('truncated')
        indices.append((repo,source_index(tree,group,modules)))
    candidates=[]
    with (output/'java_remaining_targets.csv').open(encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            found=[]
            if row['event_type']!='import':
                continue
            for repo,index in indices:
                hits,kind=match_target(row['target'],index)
                if hits and kind=='class':
                    found.extend((h['coordinate'],repo,h['path']) for h in hits)
            if len({x[0] for x in found})==1:
                coordinate,repo,path=found[0]
                candidates.append([row['target'],coordinate,row['episodes'],repo,path])
    with (output/'java_cached_class_candidates.csv').open('w',newline='',encoding='utf-8') as handle:
        out=csv.writer(handle);out.writerow(['target','coordinate','episodes','repo','source_path']);out.writerows(candidates)
    return dict(targets=len(candidates),target_episode_incidences=sum(int(r[2]) for r in candidates))


def main():
    output=BASE/'results_remaining_review'
    output.mkdir(exist_ok=True)
    reasons,queries,tokens=Counter(),Counter(),Counter()
    unresolved=set()
    for row in read_rows(BASE/'results_final/episodes.csv.gz'):
        if row['label']!='insufficient_evidence':
            continue
        eco,reason=row['ecosystem'],row['reason']
        reasons[eco,reason]+=1
        if reason=='unsupported_query_syntax':
            queries[eco,row['query_kind'],row['query_value']]+=1
        for token in set(row['upstream_reasons'].split('|'))-{'','mapping_uncertain'}:
            tokens[eco,token]+=1
        if eco=='Maven' and reason=='upstream_identity_unresolved':
            unresolved.add(row['episode_id'])
    events=defaultdict(set)
    for row in read_rows(BASE/'results_java_full/episode_event_links.csv.gz'):
        if row['episode_id'] in unresolved:
            events[row['event_id']].add(row['episode_id'])
    targets=defaultdict(set)
    db=read_db(BASE/'inputs/java_stage2.sqlite')
    for row in db.execute('SELECT id,body FROM events'):
        if row['id'] in events:
            event=unpack(row['body'])
            targets[event['event_type'],event['raw_target']].update(events[row['id']])
    save(output/'reasons.csv',['ecosystem','reason'],reasons)
    save(output/'unsupported_queries.csv',['ecosystem','kind','query'],queries)
    save(output/'upstream_reason_tokens.csv',['ecosystem','token'],tokens)
    save(output/'java_remaining_targets.csv',['event_type','target'],Counter({k:len(v) for k,v in targets.items()}))
    summary=dict(insufficient=sum(reasons.values()),identity_unresolved=sum(n for (_,r),n in reasons.items() if r=='upstream_identity_unresolved'),
                 unsupported_queries=sum(queries.values()),source='results_final/episodes.csv.gz',
                 note='Diagnostic counts; target and reason-token groups can overlap. No label or evidence changes.')
    summary['cached_java_candidates']=cached_java_candidates(output)
    write_json(output/'summary.json',summary)
    print(summary)


if __name__=='__main__':
    main()

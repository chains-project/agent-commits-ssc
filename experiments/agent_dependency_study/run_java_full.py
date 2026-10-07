"""Replay every Java commit through repaired Stage2 and saved-evidence classification."""
import argparse
import csv
import gzip
import json
import sys
import time
from collections import Counter,defaultdict
from functools import lru_cache
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'src'),str(BASE/'scripts/hallucination'),str(BASE/'vendor'),str(BASE)]
from common import digest,read_db,unpack,write_json
from stage2_episode_linker import build_episode_tables,_select_context
from stage2_java_identity import resolve_import,catalog,rule_for
from stage3_classify import classify,collect_facts
from stage3_matching import Matcher
from stage3_run import writer,write_counts


def baseline_rows():
    with gzip.open(BASE/'results/episodes.csv.gz','rt',encoding='utf-8') as handle:
        return {r['episode_id']:r for r in csv.DictReader(handle) if r['ecosystem']=='Maven'}


class Evaluation:
    def __init__(self,db,old):
        self.db,self.old,self.matcher=db,old,Matcher()

    @lru_cache(maxsize=8192)
    def observations(self,package):
        return [(r['id'],unpack(r['body'])) for r in self.db.execute(
            "SELECT id,body FROM observations WHERE eco='Maven' AND pkg=? ORDER BY id",(package,))]

    @lru_cache(maxsize=2048)
    def facts(self,package,kind,value,exclude):
        query=dict(ecosystem='Maven',query_kind=kind,query_value=value)
        return collect_facts(query,self.observations(package),self.matcher,exclude)

    def evaluate(self,episode,commit):
        eid=episode['episode_id']
        previous=self.old.get(eid,{})
        saved=self.db.execute('SELECT body FROM auxiliary WHERE eid=?',(eid,)).fetchone()
        aux=unpack(saved['body']) if saved else {}
        target=aux.get('effective_package',episode['package_name'])
        boundary=commit['committer'] or commit['author']
        records=self.observations(target)
        queryable=episode['query_kind'] in {'name_only','exact','range'}
        args=(target,episode['query_kind'],episode['query_value'])
        value=classify(episode,records,boundary,self.matcher,aux,
            prepared_facts=self.facts(*args,False) if queryable else None)
        without=classify(episode,records,boundary,self.matcher,aux,exclude_proxies=True,
            prepared_facts=self.facts(*args,True) if queryable else None)
        value.update(episode_id=eid,repo=commit['repo'],sha=commit['sha'],ecosystem='Maven',
            package_name=episode['package_name'],effective_package=target,query_kind=episode['query_kind'],
            query_value=episode['query_value'],baseline_queryable=previous.get('baseline_queryable','0'),
            final_queryable=str(int(queryable)),baseline_id_present=str(eid in self.old),
            boundary=boundary,boundary_source='committer' if commit['committer'] else 'author_fallback',
            evidence_ids='|'.join(value['evidence_ids']),auxiliary_ref=eid if aux else '',
            upstream_reasons=episode['reason_codes'],proxy_excluded_label=without['label'],
            proxy_excluded_subtype=without['subtype'],languages='Java',
            historical_advisory_flag=previous.get('historical_advisory_flag','False'))
        return value


def open_csv(path,fields):
    handle,raw=writer(path)
    csvout=csv.DictWriter(handle,fieldnames=fields,extrasaction='ignore')
    csvout.writeheader()
    return handle,raw,csvout


def bridge_rows(old_links,new_links,old,new):
    oldmap,newmap=defaultdict(set),defaultdict(set)
    for row in old_links:
        oldmap[row['event_id']].add(row['eid'])
    for row in new_links:
        newmap[row['event_id']].add(row['episode_id'])
    assert set(oldmap)==set(newmap)
    for event in sorted(oldmap):
        for before in sorted(oldmap[event]):
            for after in sorted(newmap[event]):
                a,b=old[before],new[after]
                yield dict(event_id=event,old_episode=before,new_episode=after,
                    old_kind=a['query_kind'],new_kind=b['query_kind'],old_package=a['package_name'],
                    new_package=b['package_name'],old_label=a['label'],new_label=b['label'])


def execute(inputs,evidence,output,old):
    evaluator=Evaluation(evidence,old)
    fields=list(next(iter(old.values())))+['final_queryable','baseline_id_present','languages']
    handles=[open_csv(output/'episodes.csv.gz',fields),
        open_csv(output/'event_bridge.csv.gz',['event_id','old_episode','new_episode','old_kind','new_kind','old_package','new_package','old_label','new_label']),
        open_csv(output/'episode_event_links.csv.gz',['episode_id','event_id','link_method','schema_version'])]
    counts,reasons=Counter(),Counter()
    recovered,seen=set(),set()
    event_count=0
    with gzip.open(output/'mapping_witnesses.jsonl.gz','wt',encoding='utf-8') as witnesses:
        for number,row in enumerate(inputs.execute('SELECT * FROM commits ORDER BY id'),1):
            commit=unpack(row['body'])
            cid=commit['commit_id']
            events=[unpack(r['body']) for r in inputs.execute('SELECT body FROM events WHERE cid=? ORDER BY id',(cid,))]
            contexts=unpack(inputs.execute('SELECT body FROM contexts WHERE cid=?',(cid,)).fetchone()['body'])
            episodes,links=build_episode_tables([commit],events,contexts)
            times=dict(evidence.execute('SELECT * FROM commits WHERE id=?',(cid,)).fetchone())
            records={r['episode_id']:evaluator.evaluate(r,times) for r in episodes}
            assert not seen.intersection(records)
            seen.update(records)
            for record in records.values():
                handles[0][2].writerow(record)
                counts[record['final_queryable'],record['label'],record['subtype']]+=1
                reasons[record['label'],record['reason']]+=1
            handles[2][2].writerows(links)
            oldlinks=inputs.execute('SELECT l.* FROM old_links l JOIN old_episodes e ON e.id=l.eid WHERE e.cid=?',(cid,))
            for bridge in bridge_rows(oldlinks,links,old,records):
                handles[1][2].writerow(bridge)
                if bridge['old_kind']=='unresolved' and bridge['new_kind'] in {'exact','range','name_only'}:
                    recovered.add(bridge['old_episode'])
            write_witnesses(events,contexts,links,witnesses)
            event_count+=len(events)
            if number%1000==0:
                print('Java commits',number,'episodes',len(seen),flush=True)
    for handle,raw,_ in handles:
        handle.close(); raw.close()
    evaluator.matcher.close()
    write_counts(output/'label_counts.csv',['final_queryable','label','subtype'],counts)
    write_counts(output/'reasons.csv',['label','reason'],reasons)
    return dict(commits=number,events=event_count,episodes=len(seen),recovered_old_unresolved=len(recovered),
        labels=dict(Counter({label:sum(v for k,v in counts.items() if k[1]==label) for label in {k[1] for k in counts}})),
        queryable=sum(v for k,v in counts.items() if k[0]=='1'))


def write_witnesses(events,contexts,links,handle):
    manifests=[r for r in events if r['event_type']=='manifest_addition']
    newmap=defaultdict(list)
    for row in links:
        newmap[row['event_id']].append(row['episode_id'])
    for event in events:
        if not rule_for(event):
            continue
        resolved=resolve_import(event,manifests,contexts,_select_context(event,contexts))
        if resolved:
            row=dict(event_id=event['event_id'],episode_ids=sorted(newmap[event['event_id']]),**resolved['witness'])
            handle.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=BASE/'inputs/java_stage2.sqlite')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    expected=json.loads(args.input.with_suffix('.manifest.json').read_text())
    assert digest(args.input)==expected['sha256']
    old=baseline_rows()
    assert len(old)==177735
    started=time.monotonic()
    summary=execute(read_db(args.input),read_db(BASE/'inputs/observations.sqlite'),args.output,old)
    assert summary['commits']==12466 and summary['events']==541169
    summary.update(elapsed_seconds=time.monotonic()-started,old_episodes=len(old),
        old_queryable=sum(int(r['baseline_queryable']) for r in old.values()),
        old_labels=dict(Counter(r['label'] for r in old.values())),
        input_sha256=expected['sha256'],registry_input_sha256=json.loads((BASE/'inputs/manifest.json').read_text())['input_sha256'],
        catalog_sha256=digest(BASE/'scripts/hallucination/java_class_catalog.json'),
        catalog_targets=len(catalog()),catalog_artifacts=len({r['coordinate'] for r in catalog().values()}))
    summary['output_sha256']={p.name:digest(p) for p in args.output.iterdir() if p.is_file()}
    write_json(args.output/'summary.json',summary)
    print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__':
    main()

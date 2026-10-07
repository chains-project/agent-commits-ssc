"""Replay Java Stage2 and final classification with event-level before/after bridges."""
import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'src'),str(BASE/'scripts/hallucination'),str(BASE/'vendor'),str(BASE)]
from common import csv_rows,digest,write_json
from audit_insufficient import write_csv
from stage2_episode_linker import build_episode_tables,_select_context
from stage2_java_identity import resolve_import
import stage2_java_identity
from stage3_classify import classify
from stage3_matching import Matcher


def load(path,name):
    return json.loads((path/name).read_text(encoding='utf-8'))


def classify_rows(episodes,times,aux,observations,matcher):
    rows=[]
    for episode in episodes:
        context=aux.get(episode['episode_id'],{})
        target=context.get('effective_package',episode['package_name'])
        commit=times[episode['commit_id']]
        boundary=commit['committer'] or commit['author']
        result=classify(episode,observations.get(target,[]),boundary,matcher,context)
        rows.append(dict(episode,label=result['label'],final_reason=result['reason'],
            evidence_ids='|'.join(result['evidence_ids']),boundary=boundary,
            boundary_source='committer' if commit['committer'] else 'author_fallback',
            matching_version=result['matching_version'],delta_seconds=result['delta_seconds']))
    return rows


def event_bridge(old_links,new_links,old_rows,new_rows):
    old_by={r['episode_id']:r for r in old_rows}
    new_by={r['episode_id']:r for r in new_rows}
    old_events,new_events=defaultdict(set),defaultdict(set)
    for row in old_links:
        old_events[row['event_id']].add(row['episode_id'])
    for row in new_links:
        new_events[row['event_id']].add(row['episode_id'])
    assert set(old_events)==set(new_events)
    pairs=[]
    for event in sorted(old_events):
        for old in sorted(old_events[event]):
            for new in sorted(new_events[event]):
                a,b=old_by[old],new_by[new]
                pairs.append(dict(event_id=event,old_episode=old,new_episode=new,
                    old_package=a['package_name'],new_package=b['package_name'],
                    old_kind=a['query_kind'],new_kind=b['query_kind'],
                    old_label=a['label'],new_label=b['label']))
    return pairs


def witnesses(commits,events,contexts,new_links):
    commit_keys={r['commit_id']:(r['repo'].lower(),r['sha'].lower()) for r in commits}
    grouped,ctx=defaultdict(list),defaultdict(list)
    for event in events:
        grouped[event['commit_id']].append(event)
    for row in contexts:
        ctx[row['repo'].lower(),row['sha'].lower()].append(row)
    links=defaultdict(list)
    for row in new_links:
        links[row['event_id']].append(row['episode_id'])
    for cid in sorted(grouped):
        group=grouped[cid]
        rows=ctx[commit_keys[cid]]
        manifests=[r for r in group if r['event_type']=='manifest_addition']
        for event in sorted(group,key=lambda r:r['event_id']):
            resolved=resolve_import(event,manifests,rows,_select_context(event,rows))
            if resolved:
                yield dict(event_id=event['event_id'],episode_ids=sorted(links[event['event_id']]),**resolved['witness'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=BASE/'samples/java_identity')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if (args.input/'catalog.json').exists():
        stage2_java_identity.CATALOG_PATH=args.input/'catalog.json'
        stage2_java_identity.catalog.cache_clear()
    args.output.mkdir(parents=True,exist_ok=False)
    manifest=load(args.input,'manifest.json')
    for name,sha in manifest['inputs'].items():
        assert digest(args.input/name)==sha,name
    commits=list(csv_rows(args.input/'commits.csv'))
    events=list(csv_rows(args.input/'events.csv'))
    contexts=load(args.input,'contexts.json')
    started=time.monotonic()
    episodes,links=build_episode_tables(commits,events,contexts)
    observations=defaultdict(list)
    for row in load(args.input,'observations.json'):
        observations[row['record']['package_name']].append((row['id'],row['record']))
    times,aux=load(args.input,'commit_times.json'),load(args.input,'auxiliary.json')
    matcher=Matcher()
    old=classify_rows(list(csv_rows(args.input/'old_episodes.csv')),times,aux,observations,matcher)
    new=classify_rows(episodes,times,aux,observations,matcher)
    matcher.close()
    bridge=event_bridge(list(csv_rows(args.input/'old_links.csv')),links,old,new)
    recovered={r['old_episode'] for r in bridge if r['old_kind']=='unresolved' and r['new_kind'] in {'exact','range','name_only'}}
    witness_rows=list(witnesses(commits,events,contexts,links))
    for name,rows in [('old_classification',old),('new_classification',new),('new_episode_event_links',links),('event_bridge',bridge)]:
        write_csv(args.output/(name+'.csv'),rows)
    write_json(args.output/'mapping_witnesses.json',witness_rows)
    summary=dict(commits=len(commits),events=len(events),old_episodes=len(old),new_episodes=len(new),
        old_labels=dict(Counter(r['label'] for r in old)),new_labels=dict(Counter(r['label'] for r in new)),
        recovered_old_unresolved_ids=len(recovered),catalog_import_events=len(witness_rows),
        elapsed_seconds=time.monotonic()-started,
        catalog_sha256=digest(stage2_java_identity.CATALOG_PATH),
        event_link_closure=True,selection=manifest['selection'])
    write_json(args.output/'summary.json',summary)
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    main()

"""Rebuild global Stage2 from retained imports and verified manifest-snapshot repair."""
import argparse
import gzip
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
BASE=Path(__file__).resolve().parent
ROOT=BASE.parents[1]
sys.path[:0]=[str(BASE/'src'),str(BASE/'scripts/hallucination'),str(BASE/'vendor')]
from common import read_db,unpack,pack,csv_rows,digest,write_json
from stage1_replay_adapter import apply_snapshots,new_commit,refresh_commit
from stage2_episode_linker import build_episode_tables
from stage2_context import SqliteContextLookup,derived_context_rows
from stage_schema import make_commit_id

CONTEXT=Path(r'D:\MasterThesis\thesis-work-data\data_products\rq2_phase9_hardened_full_20260811_v1\context')
SCHEMA='''
CREATE TABLE commits(id TEXT PRIMARY KEY,body BLOB,events_changed INTEGER,new_commit INTEGER);
CREATE TABLE events(cid TEXT PRIMARY KEY,body BLOB);
CREATE TABLE episodes(id TEXT PRIMARY KEY,cid TEXT,eco TEXT,pkg TEXT,kind TEXT,value TEXT,q INTEGER,body BLOB);
CREATE INDEX episode_commit ON episodes(cid);
CREATE TABLE links(cid TEXT PRIMARY KEY,body BLOB);
'''


def population_for_new(db):
    missing={r[0] for r in db.execute('SELECT p.cid FROM patches p LEFT JOIN commits c ON c.id=p.cid WHERE c.id IS NULL')}
    found={}
    path=ROOT/'data_products/agent_commit_population_merged_1y_v1/merged_agent_commit_population.csv'
    for row in csv_rows(path):
        cid=make_commit_id(row['repo'],row['sha'])
        if cid in missing and cid not in found:found[cid]=row
    return found


def contexts_for(commit,events,lookups):
    language_map={'npm':['TypeScript','JavaScript'],'PyPI':['Python'],'Maven':['Java'],'Cargo':['Rust'],'Go':['Go']}
    wanted={lang for row in events for lang in language_map.get(row['ecosystem'],[])}
    unique={}
    for language in sorted(wanted):
        for row in lookups[language].lookup(commit):
            unique[json.dumps(row,sort_keys=True)]=row
    return list(unique.values())+derived_context_rows(commit,events)


def execute(source,output,lookups,population,counts,witness_handle):
    ids=source.execute('SELECT id AS cid FROM commits UNION SELECT cid FROM patches ORDER BY cid')
    for number,record in enumerate(ids,1):
        cid=record['cid'];saved=source.execute('SELECT body FROM commits WHERE id=?',(cid,)).fetchone()
        if not saved and cid not in population:counts['outside_population_skipped']+=1;continue
        before=source.execute('SELECT body FROM events WHERE cid=?',(cid,)).fetchone()
        old=unpack(before['body']) if before else []
        witnesses=[]
        events,index,changed=apply_snapshots(source,cid,old,ROOT,counts,witnesses)
        if not events:counts['empty_after_extraction']+=1;continue
        commit=unpack(saved['body']) if saved else new_commit(cid,index,population[cid],events)
        commit=refresh_commit(commit,events)
        episodes,links=build_episode_tables([commit],events,contexts_for(commit,events,lookups))
        assert {r['event_id'] for r in events}=={r['event_id'] for r in links},cid
        assert len({r['episode_id'] for r in episodes})==len(episodes)
        output.execute('INSERT INTO commits VALUES(?,?,?,?)',(cid,pack(commit),int(changed),int(not saved)))
        output.execute('INSERT INTO events VALUES(?,?)',(cid,pack(events)))
        output.execute('INSERT INTO links VALUES(?,?)',(cid,pack(links)))
        output.executemany('INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?)',[(r['episode_id'],cid,r['ecosystem'],r['package_name'],r['query_kind'],r['query_value'],int(r['query_kind'] in {'exact','range','name_only'}),pack(r)) for r in episodes])
        oldids={r['event_id'] for r in old};newids={r['event_id'] for r in events}
        counts.update(commits=1,events=len(events),episodes=len(episodes),links=len(links),
            events_added=len(newids-oldids),events_removed=len(oldids-newids),events_changed_commits=int(changed),new_commits=int(not saved))
        counts['queryable']+=sum(r['query_kind'] in {'exact','range','name_only'} for r in episodes)
        for witness in witnesses:witness_handle.write(json.dumps(witness,sort_keys=True)+'\n')
        if number%1000==0:
            output.commit();print('Stage12 commits',number,'episodes',counts['episodes'],'added events',counts['events_added'],flush=True)
    output.commit()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=BASE/'inputs/replay.sqlite')
    parser.add_argument('--output',type=Path,default=BASE/'results_replay')
    args=parser.parse_args();args.output.mkdir(exist_ok=False)
    source=read_db(args.input)
    lookups={lang:SqliteContextLookup(CONTEXT/(lang+'.sqlite')) for lang in ['TypeScript','JavaScript','Python','Java','Go','Rust']}
    population=population_for_new(source)
    output=sqlite3.connect(args.output/'stage12.sqlite');output.executescript(SCHEMA)
    output.execute('PRAGMA journal_mode=WAL');counts=Counter();start=time.monotonic()
    with gzip.open(args.output/'manifest_witnesses.jsonl.gz','wt',encoding='utf-8') as handle:
        execute(source,output,lookups,population,counts,handle)
    output.execute('PRAGMA wal_checkpoint(TRUNCATE)');output.close()
    for lookup in lookups.values():lookup.close()
    write_json(args.output/'stage12_summary.json',dict(counts=counts,elapsed_seconds=time.monotonic()-start,
        input_sha256=digest(args.input),output_sha256=digest(args.output/'stage12.sqlite'),
        manifest_witnesses_sha256=digest(args.output/'manifest_witnesses.jsonl.gz')))
    print(json.dumps(counts),flush=True)


if __name__=='__main__':main()

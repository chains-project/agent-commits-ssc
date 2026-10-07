"""Prepare one offline commit/event universe and child snapshot index for replay.

Original source tables and caches are read-only. The exported database deduplicates
cross-language event IDs and includes fetched-manifest commits absent from Stage1.
"""
import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from itertools import groupby
from pathlib import Path
BASE=Path(__file__).resolve().parent
ROOT=BASE.parents[1]
sys.path[:0]=[str(BASE/'src'),str(BASE/'scripts/hallucination')]
from common import csv_rows,digest,pack,unpack,write_json
from stage_schema import make_commit_id
from stage1_manifest_diff import manifest_descriptor,is_research_file_path

DATA=Path(r'D:\MasterThesis\thesis-work-data')
CANONICAL=ROOT/'data_products/rq2_phase9_targeted_pep508_six_language_20260816_v2/canonical_source_manifest.json'
WAVE=DATA/'data_products/rq2_hallucinated_unlimited_overnight_20260710/wave01_nall'
INDEX=DATA/'diff_corpus/commit_json_diffs_by_language/commit_index.csv'
SCHEMA='''
CREATE TABLE commits(id TEXT PRIMARY KEY,repo TEXT,sha TEXT,body BLOB,languages TEXT);
CREATE TABLE events(cid TEXT PRIMARY KEY,body BLOB);
CREATE TABLE snapshots(cid TEXT,path TEXT,evidence TEXT,PRIMARY KEY(cid,path));
CREATE TABLE patches(cid TEXT PRIMARY KEY,body BLOB);
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
'''


def merge_commit(db,row,language):
    previous=db.execute('SELECT body,languages FROM commits WHERE id=?',(row['commit_id'],)).fetchone()
    langs={language}
    if previous:
        old=unpack(previous[0]);langs.update(previous[1].split('|'))
        assert all(old[k]==row[k] for k in ['repo','sha','author_date']),row['commit_id']
        row=old
    db.execute('INSERT OR REPLACE INTO commits VALUES(?,?,?,?,?)',
        (row['commit_id'],row['repo'],row['sha'],pack(row),'|'.join(sorted(langs))))


def merge_events(db,cid,rows):
    previous=db.execute('SELECT body FROM events WHERE cid=?',(cid,)).fetchone()
    merged={r['event_id']:r for r in unpack(previous[0])} if previous else {}
    for row in rows:
        if row['event_id'] in merged:
            assert merged[row['event_id']]==row,(cid,row['event_id'])
        else:
            merged[row['event_id']]=row
    db.execute('INSERT OR REPLACE INTO events VALUES(?,?)',(cid,pack(sorted(merged.values(),key=lambda r:r['event_id']))))


def source_events(db,manifest):
    sources=[]
    for language,item in manifest['languages'].items():
        base=Path(item['selected_path'])
        for name in ['commits.csv','events.csv']:
            assert digest(base/name)==item['tables'][name]['sha256'],str(base/name)
            sources.append(dict(path=str(base/name),sha256=item['tables'][name]['sha256']))
        for row in csv_rows(base/'commits.csv'):
            merge_commit(db,row,language)
        for n,(cid,rows) in enumerate(groupby(csv_rows(base/'events.csv'),lambda r:r['commit_id']),1):
            merge_events(db,cid,rows)
            if n%10000==0:db.commit()
        db.commit()
        print('Prepared events',language,flush=True)
    return sources


def snapshot_index(db,languages):
    counts=Counter()
    for language in languages:
        path=WAVE/language/'manifest_registry/manifest_fetch_results.csv'
        for row in csv_rows(path):
            name=row['manifest_path']
            if row['fetch_status']!='ok' or not manifest_descriptor(name) or not is_research_file_path(name):
                continue
            if row.get('child_sha') and row['child_sha']!=row['sha']:
                counts['wrong_child_sha']+=1;continue
            cid=make_commit_id(row['repo'],row['sha'])
            db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?)',(cid,name,row['evidence_path_or_url']))
            counts['fetch_manifest_rows']+=1
        db.commit()
    return counts


def patch_index(db):
    wanted={r[0] for r in db.execute('SELECT DISTINCT cid FROM snapshots')}
    counts=Counter()
    for row in csv_rows(INDEX):
        if row['status']!='ok':continue
        counts['ok_index_rows']+=1
        cid=make_commit_id(row['repo'],row['sha'])
        if cid in wanted:
            db.execute('INSERT OR IGNORE INTO patches VALUES(?,?)',(cid,pack(row)))
            counts['snapshot_index_rows']+=1
        if counts['ok_index_rows']%100000==0:
            db.commit();print('Index scanned',counts['ok_index_rows'],flush=True)
    db.commit()
    return counts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=BASE/'inputs/replay.sqlite')
    args=parser.parse_args()
    if args.output.exists():raise FileExistsError(args.output)
    db=sqlite3.connect(args.output);db.executescript(SCHEMA)
    db.execute('PRAGMA journal_mode=WAL')
    manifest=json.loads(CANONICAL.read_text(encoding='utf-8'))
    sources=source_events(db,manifest)
    counts=snapshot_index(db,manifest['languages'])
    counts.update(patch_index(db))
    counts.update({t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['commits','events','snapshots','patches']})
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.close()
    write_json(args.output.with_suffix('.manifest.json'),dict(counts=counts,sources=sources,
        sha256=digest(args.output),canonical=str(CANONICAL),patch_index=str(INDEX),wave=str(WAVE)))
    print(json.dumps(counts),flush=True)


if __name__=='__main__':main()

"""Export complete Java extraction/context inputs to a compact portable SQLite file."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'src'),str(BASE/'scripts/hallucination')]
from common import csv_rows,digest,pack,write_json
from stage2_context import SqliteContextLookup


SCHEMA='''
CREATE TABLE commits(id TEXT PRIMARY KEY,body BLOB);
CREATE TABLE events(id TEXT PRIMARY KEY,cid TEXT,body BLOB);
CREATE INDEX event_commit ON events(cid);
CREATE TABLE contexts(cid TEXT PRIMARY KEY,body BLOB);
CREATE TABLE old_episodes(id TEXT PRIMARY KEY,cid TEXT,body BLOB);
CREATE INDEX old_commit ON old_episodes(cid);
CREATE TABLE old_links(eid TEXT,event_id TEXT);
CREATE INDEX old_link_episode ON old_links(eid);
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['source','context','output']:
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(args.output)
    db.executescript(SCHEMA)
    commits=list(csv_rows(args.source/'commits.csv'))
    db.executemany('INSERT INTO commits VALUES(?,?)',[(r['commit_id'],pack(r)) for r in commits])
    db.executemany('INSERT INTO events VALUES(?,?,?)',((r['event_id'],r['commit_id'],pack(r)) for r in csv_rows(args.source/'events.csv')))
    db.executemany('INSERT INTO old_episodes VALUES(?,?,?)',((r['episode_id'],r['commit_id'],pack(r)) for r in csv_rows(args.source/'episodes.csv')))
    db.executemany('INSERT INTO old_links VALUES(?,?)',((r['episode_id'],r['event_id']) for r in csv_rows(args.source/'episode_event_links.csv')))
    db.commit()
    lookup=SqliteContextLookup(args.context)
    for n,commit in enumerate(commits,1):
        db.execute('INSERT INTO contexts VALUES(?,?)',(commit['commit_id'],pack(lookup.lookup(commit))))
        if n%2000==0:
            db.commit()
            print('Context commits',n,flush=True)
    lookup.close()
    db.commit()
    counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['commits','events','old_episodes','old_links']}
    db.close()
    files=[args.source/name for name in ['commits.csv','events.csv','episodes.csv','episode_event_links.csv']]+[args.context]
    write_json(args.output.with_suffix('.manifest.json'),dict(counts=counts,sha256=digest(args.output),
        sources=[dict(path=str(p),sha256=digest(p)) for p in files]))
    print(json.dumps(counts),flush=True)


if __name__=='__main__':
    main()

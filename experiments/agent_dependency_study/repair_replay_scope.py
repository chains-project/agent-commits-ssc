"""Rebuild only commits whose resolved import context crosses the selected manifest."""
import argparse
import json
import sqlite3
import sys
from collections import Counter,defaultdict
from pathlib import Path
BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'src'),str(BASE/'scripts/hallucination'),str(BASE)]
from common import unpack,pack,write_json,digest
from run_stage12_replay import CONTEXT,contexts_for
from stage2_context import SqliteContextLookup
from stage2_episode_linker import build_episode_tables,_select_context,_context_refines,_context_matches_selection
from stage2_alignment import select_manifests


def scope_conflict(events,contexts):
    manifests=[r for r in events if r['event_type']=='manifest_addition']
    by_path=defaultdict(list)
    for row in contexts:by_path[row.get('source_path','')].append(row)
    for event in events:
        if event['event_type']!='import':continue
        rows=by_path[event['path']]+(by_path['*'] if event['ecosystem']=='PyPI' else [])
        if event['ecosystem']=='Go':rows=contexts
        context=_select_context(event,rows)
        if not context or not _context_refines(context):continue
        selection=select_manifests(event,manifests)
        if selection.events and not _context_matches_selection(event,selection,context):return True
    return False


def rebuild(db,lookups):
    counts=Counter()
    for row in db.execute('SELECT c.id,c.body AS commit_body,e.body AS events FROM commits c JOIN events e ON e.cid=c.id ORDER BY c.id'):
        events=unpack(row['events'])
        if {r['event_type'] for r in events}!={'import','manifest_addition'}:continue
        counts['candidate_commits']+=1;commit=unpack(row['commit_body'])
        contexts=contexts_for(commit,events,lookups)
        if not scope_conflict(events,contexts):continue
        episodes,links=build_episode_tables([commit],events,contexts)
        assert {r['event_id'] for r in events}=={r['event_id'] for r in links}
        before={r['id']:unpack(r['body']) for r in db.execute('SELECT id,body FROM episodes WHERE cid=?',(row['id'],))}
        if before=={r['episode_id']:r for r in episodes}:continue
        counts.update(repaired_commits=1,old_episodes=len(before),new_episodes=len(episodes))
        db.execute('DELETE FROM episodes WHERE cid=?',(row['id'],))
        db.executemany('INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?)',[(r['episode_id'],row['id'],r['ecosystem'],r['package_name'],r['query_kind'],r['query_value'],int(r['query_kind'] in {'exact','range','name_only'}),pack(r)) for r in episodes])
        db.execute('UPDATE links SET body=? WHERE cid=?',(pack(links),row['id']))
        if counts['repaired_commits']%100==0:db.commit();print('Scope corrected',counts['repaired_commits'],flush=True)
    db.commit();return counts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage12',type=Path,default=BASE/'results_replay_v2');args=parser.parse_args()
    path=args.stage12/'stage12.sqlite';summary=json.loads((args.stage12/'stage12_summary.json').read_text())
    original=args.stage12/'stage12_before_scope.json'
    if not original.exists():write_json(original,summary)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    lookups={lang:SqliteContextLookup(CONTEXT/(lang+'.sqlite')) for lang in ['TypeScript','JavaScript','Python','Java','Go','Rust']}
    counts=rebuild(db,lookups)
    summary['counts']['episodes']=db.execute('SELECT count(*) FROM episodes').fetchone()[0]
    summary['counts']['queryable']=db.execute('SELECT sum(q) FROM episodes').fetchone()[0]
    summary['counts']['links']=sum(len(unpack(r[0])) for r in db.execute('SELECT body FROM links'))
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.close()
    for lookup in lookups.values():lookup.close()
    summary['scope_repair']=dict(counts,linker_sha256=digest(BASE/'scripts/hallucination/stage2_episode_linker.py'))
    summary['output_sha256']=digest(path);write_json(args.stage12/'stage12_summary.json',summary)
    print(json.dumps(counts),flush=True)


if __name__=='__main__':main()

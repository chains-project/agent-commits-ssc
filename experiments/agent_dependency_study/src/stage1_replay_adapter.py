"""Incremental manifest extraction using cached exact child snapshots."""
import hashlib
import json
from collections import Counter
from pathlib import Path
from common import unpack,pack
from stage1_manifest_diff import parse_unified_diff,manifest_descriptor
from stage1_snapshot import replay_manifest
from stage1_extract import _observations,_commit_row
from cache_storage import read_cache_text


def cache_text(root,evidence):
    path=Path(evidence)
    if not path.is_absolute():path=root/path
    if str(path).endswith('.gz'):path=Path(str(path)[:-3])
    return read_cache_text(path)


def apply_snapshots(db,cid,old_events,root,counts,witnesses):
    record=db.execute('SELECT body FROM patches WHERE cid=?',(cid,)).fetchone()
    if not record:return old_events,None,False
    index=unpack(record['body']);path=Path(index['patch_path'])
    if not path.is_file():counts['patch_missing']+=1;return old_events,index,False
    patches=parse_unified_diff(path.read_text(encoding='utf-8-sig',errors='replace').splitlines())
    snapshots={r['path']:r['evidence'] for r in db.execute('SELECT path,evidence FROM snapshots WHERE cid=?',(cid,))}
    events={r['event_id']:r for r in old_events}
    if not old_events:
        events={r.to_event_row(cid)['event_id']:r.to_event_row(cid) for r in _observations(patches)}
    for patch in patches:
        if not manifest_descriptor(patch.path):continue
        counts['changed_manifest_files']+=1
        if patch.path not in snapshots:counts['manifest_snapshot_not_indexed']+=1;continue
        text,actual=cache_text(root,snapshots[patch.path])
        if text is None:counts['snapshot_cache_missing']+=1;continue
        observations,status=replay_manifest(patch,text);counts[status]+=1
        if observations is None:continue
        before={k:v for k,v in events.items() if v['event_type']=='manifest_addition' and v['path']==patch.path}
        after={r.to_event_row(cid)['event_id']:r.to_event_row(cid) for r in observations}
        for eid in before:events.pop(eid)
        events.update(after)
        witnesses.append(dict(commit_id=cid,path=patch.path,child_sha=index['sha'],cache_path=str(actual),
            child_sha256=hashlib.sha256(text.encode()).hexdigest(),added_lines=[r.new_line_number for r in patch.added_lines],
            old_events=sorted(before),new_events=sorted(after),patch_path=str(path)))
    return sorted(events.values(),key=lambda r:r['event_id']),index,event_signature(old_events)!=event_signature(events.values())


def event_signature(events):
    fields=['event_id','event_type','path','raw_target','package_candidate','version_spec','event_status']
    return sorted(tuple(r.get(k,'') for k in fields) for r in events)


def new_commit(cid,index,meta,events):
    types={r['event_type'] for r in events}
    return dict(commit_id=cid,repo=index['repo'],sha=index['sha'],author_date=meta.get('author_date',''),
        agent=meta.get('agent',''),repo_primary_language=index.get('repo_language',''),
        actual_changed_languages='|'.join(sorted({r['actual_language'] for r in events if r['event_type']=='import'})),
        import_event_count=str(sum(r['event_type']=='import' for r in events)),
        manifest_event_count=str(sum(r['event_type']=='manifest_addition' for r in events)),
        dependency_quadrant=('I+' if 'import' in types else 'I-')+('M+' if 'manifest_addition' in types else 'M-'),schema_version='2')


def refresh_commit(commit,events):
    derived=new_commit(commit['commit_id'],{'repo':commit['repo'],'sha':commit['sha']},commit,events)
    fields=('actual_changed_languages','import_event_count','manifest_event_count','dependency_quadrant')
    return {**commit,**{key:derived[key] for key in fields}}

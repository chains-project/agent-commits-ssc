"""Source-machine export of frozen inputs; full payloads, never old decisions as facts."""
import hashlib
import json
import sqlite3
from pathlib import Path
from common import csv_rows, digest, dump, pack, unpack

SCHEMA = '''
CREATE TABLE episodes(id TEXT PRIMARY KEY,cid TEXT,eco TEXT,pkg TEXT,kind TEXT,value TEXT,q INTEGER,body BLOB);
CREATE TABLE commits(id TEXT PRIMARY KEY,repo TEXT,sha TEXT,author TEXT,committer TEXT DEFAULT '',time_source TEXT DEFAULT '');
CREATE INDEX commit_identity ON commits(repo,sha);
CREATE TABLE observations(id TEXT PRIMARY KEY,eco TEXT,pkg TEXT,body BLOB);
CREATE INDEX obs_target ON observations(eco,pkg);
CREATE TABLE auxiliary(eid TEXT PRIMARY KEY,body BLOB);
CREATE TABLE old_labels(eid TEXT PRIMARY KEY,label TEXT);
CREATE TABLE sources(name TEXT PRIMARY KEY,body BLOB);
CREATE TABLE raw(id TEXT PRIMARY KEY,body BLOB);
'''


def source(db, path):
    path = Path(path)
    item = dict(path=str(path), bytes=path.stat().st_size, sha256=digest(path))
    db.execute('INSERT OR REPLACE INTO sources VALUES(?,?)', (str(path), pack(item)))
    return item


def observation(db, record, origin):
    body = dict(record, origin=origin)
    key = hashlib.sha256(dump(record).encode()).hexdigest()
    db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?)',
               (key, record['ecosystem'], record['package_name'], pack(body)))
    return key


def auxiliary(db, eid, **values):
    row = db.execute('SELECT body FROM auxiliary WHERE eid=?', (eid,)).fetchone()
    item = unpack(row[0]) if row else {}
    item.update(values)
    db.execute('INSERT OR REPLACE INTO auxiliary VALUES(?,?)', (eid, pack(item)))


def time_type(eco, source_name):
    if source_name == 'legacy_exact_registry_results':
        return 'legacy_unspecified_time'
    return {'npm':'npm_version_time','PyPI':'pypi_upload_time','Cargo':'cargo_created_at',
            'Go':'go_module_time','Maven':'maven_index_timestamp'}[eco]


def load_evidence(db, base):
    metadata = {}
    for row in json_lines(base/'evidence.jsonl'):
        if row['evidence_type'] == 'registry':
            metadata[row['query_key']] = row['payload_sha256']
    for row in json_lines(base/'stage3_evidence_input.jsonl'):
        if row['evidence_type'] != 'registry':
            continue
        key = 'registry|'+row['ecosystem']+'|'+row['package_name']
        if key not in metadata:
            continue
        raw_hash = hashlib.sha256(dump(row).encode()).hexdigest()
        assert raw_hash == metadata[key], key
        item = dict(row, time_type=time_type(row['ecosystem'], row['source']))
        item['scope'] = 'observed_subset' if row.get('reuse_scope') else 'enumeration'
        item['negative_kind'] = 'package'
        observation(db, item, dict(file=str(base/'stage3_evidence_input.jsonl'), payload_sha256=raw_hash))
    source(db, base/'evidence.jsonl')
    source(db, base/'stage3_evidence_input.jsonl')


def json_lines(path):
    with path.open(encoding='utf-8-sig') as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def load_commits(db, path):
    for row in csv_rows(path):
        values = tuple(row[k] for k in ('commit_id','repo','sha','author_date'))
        old = db.execute('SELECT repo,sha,author FROM commits WHERE id=?', values[:1]).fetchone()
        if old:
            assert tuple(old) == values[1:]
        else:
            db.execute('INSERT INTO commits(id,repo,sha,author) VALUES(?,?,?,?)', values)


def load_episodes(db, path, lang):
    for row in csv_rows(path):
        eid = row['episode_id']
        core = tuple(row[k] for k in ('commit_id','ecosystem','package_name','query_kind','query_value'))
        old = db.execute('SELECT cid,eco,pkg,kind,value,body FROM episodes WHERE id=?', (eid,)).fetchone()
        if old:
            assert tuple(old[:5]) == core, eid
            body = unpack(old[5])
            body['languages'] = sorted(set(body['languages']) | {lang})
            iq = 'I+' if 'I+' in body['dependency_quadrant'] or 'I+' in row['dependency_quadrant'] else 'I-'
            mq = 'M+' if 'M+' in body['dependency_quadrant'] or 'M+' in row['dependency_quadrant'] else 'M-'
            body['dependency_quadrant'] = iq + mq
            body['reason_codes'] = '|'.join(sorted(set(body['reason_codes'].split('|')) | set(row['reason_codes'].split('|'))))
            body['source_evidence_ids'] = sorted(set(body['source_evidence_ids']) | set(filter(None,row['evidence_ids'].split('|'))))
            db.execute('UPDATE episodes SET body=? WHERE id=?', (pack(body), eid))
        else:
            body = dict(row, languages=[lang], source_evidence_ids=list(filter(None,row['evidence_ids'].split('|'))))
            q = int(row['query_kind'] in {'exact','range','name_only'})
            db.execute('INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?)', (eid,*core,q,pack(body)))


def load_canonical(db, canonical):
    manifest_path = canonical/'canonical_source_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    source(db, manifest_path)
    for lang, item in manifest['languages'].items():
        base = Path(item['selected_path'])
        for name, loader in [('commits.csv',load_commits),('episodes.csv',load_episodes)]:
            info = source(db, base/name)
            assert info['sha256'] == item['tables'][name]['sha256'], str(base/name)
            loader(db, base/name, lang) if name == 'episodes.csv' else loader(db,base/name)
        load_evidence(db, base)
        db.commit()
        print('Prepared', lang, flush=True)

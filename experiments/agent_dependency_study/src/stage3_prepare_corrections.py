"""Adapt raw cached rechecks and bound source rows; never use final labels as evidence."""
import gzip
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from common import csv_rows, dump, pack
from stage3_prepare_base import auxiliary, observation, source, time_type


def raw_cache(db, path):
    path = Path(path)
    if not path.is_file():
        return None, dict(missing_cache=str(path))
    data = gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_bytes()
    value = json.loads(data)
    key = hashlib.sha256(data).hexdigest()
    db.execute('INSERT OR IGNORE INTO raw VALUES(?,?)', (key, pack(value)))
    return value, dict(raw_id=key, cache_path=str(path))


def releases(eco, body):
    payload = json.loads(body) if isinstance(body, str) else body
    if eco == 'npm':
        return [dict(version=v,published_at=payload.get('time',{}).get(v,'')) for v in payload.get('versions',{})]
    if eco == 'PyPI':
        return [dict(version=v,published_at=min([f.get('upload_time_iso_8601') or f.get('upload_time','') for f in fs if f.get('upload_time_iso_8601') or f.get('upload_time')] or [''])) for v,fs in payload.get('releases',{}).items()]
    if eco == 'Cargo':
        return [dict(version=r['num'],published_at=r.get('created_at','')) for r in payload.get('versions',[]) if r.get('num')]
    if eco == 'Go':
        return [dict(version=payload.get('Version',''),published_at=payload.get('Time',''))] if payload.get('Version') else []
    return []


def requery(db, path):
    source(db,path)
    for row in csv_rows(path):
        target = row['effective_package_name']
        auxiliary(db,row['episode_id'],effective_package=target,requery_source_row=row)
        wrapper, origin = raw_cache(db,row['cache_path'])
        if wrapper is None:
            continue
        status = int(wrapper.get('status',0))
        eco = row['ecosystem']
        lookup = 'ok' if status == 200 else 'not_found' if status == 404 else 'error'
        record = dict(ecosystem=eco,package_name=target,lookup_status=lookup,
            source=row['registry_url'],queried_at=row['retrieved_utc'],
            versions=releases(eco,wrapper['body']) if status == 200 else [],
            time_type=time_type(eco,''),scope='exact' if eco=='Go' else 'enumeration',
            exact_query=row['query_value'] if eco=='Go' else '',
            negative_kind='version' if eco=='Go' else 'package')
        observation(db,record,dict(origin,source_row=row))


def commit_times(db, path):
    if not path.is_file():
        return
    source(db,path)
    for row in csv_rows(path):
        stamp = row.get('github_committer_date','')
        if stamp and row.get('commit_status','200') == '200':
            db.execute('UPDATE commits SET committer=?,time_source=? WHERE repo=? AND sha=?',
                       (stamp, str(path.name), row['repo'],row['sha']))


def maven_metadata(db,path):
    source(db,path)
    for row in csv_rows(path):
        for provider, cache in json.loads(row['cache_paths_json']).items():
            wrapper, origin = raw_cache(db,cache)
            if wrapper is None:
                continue
            status = int(wrapper.get('status',0))
            versions = []
            if status == 200:
                try:
                    root = ET.fromstring(wrapper['body'])
                    versions = [dict(version=n.text.strip(),published_at='') for n in root.findall('./versioning/versions/version') if n.text]
                except ET.ParseError:
                    status = 0
            record = dict(ecosystem='Maven',package_name=row['package_name'],
                source=provider,lookup_status='ok' if status==200 else 'not_found' if status==404 else 'error',
                versions=versions,time_type='maven_metadata_no_time',scope='enumeration',negative_kind='package',
                queried_at=json.loads(row['retrieved_utc_json']).get(provider,''))
            observation(db,record,dict(origin,source_row=row))


def maven_times(db,path):
    source(db,path)
    for row in csv_rows(path):
        if row['query_kind'] != 'exact':
            continue
        for head in json.loads(row['artifact_head_evidence_json']):
            if head.get('status') != 200:
                continue
            from email.utils import parsedate_to_datetime
            value = head.get('last_modified','')
            stamp = parsedate_to_datetime(value).isoformat() if value else ''
            record = dict(ecosystem='Maven',package_name=row['package_name'],source=head['source']+'_pom',
                lookup_status='ok',versions=[dict(version=row['query_value'],published_at=stamp)],
                scope='exact',exact_query=row['query_value'],time_type='http_last_modified',negative_kind='version')
            observation(db,record,dict(source_row=row,head=head))


@lru_cache(maxsize=64)
def added_lines(path):
    result, filename, line = {}, '', 0
    if not Path(path).is_file():
        return result
    for text in Path(path).read_text(encoding='utf-8',errors='replace').splitlines():
        if text.startswith('+++ b/'):
            filename = text[6:]
        elif text.startswith('@@ '):
            match = re.search(r'\+(\d+)',text)
            line = int(match.group(1)) if match else 0
        elif text.startswith('+') and not text.startswith('+++'):
            result[(filename,line)] = text[1:]
            line += 1
        elif text.startswith(' '):
            line += 1
    return result


def bound_scope(db,path):
    source(db,path)
    count = 0
    for row in csv_rows(path):
        events = json.loads(row['events_json'])
        manifest = [e for e in events if e['event_type']=='manifest_addition']
        if not manifest:
            continue
        lines = added_lines(row['patch_path'])
        witnesses = [dict(event=e,text=lines.get((e['path'],int(e['new_line_number'])),'')) for e in manifest]
        local = [bool(re.search(r'\bpath\s*=|workspace\s*=\s*true|(?:workspace:|file:|link:|git\+)',w['text'])) for w in witnesses]
        auxiliary(db,row['episode_id'],local_only=all(local),mixed_scope=any(local) and not all(local),
                  manifest_witnesses=witnesses,scope_source=str(path.name))
        count += all(local)
    print('Bound local-only episode witnesses:',count,flush=True)


def python_scope(db,path):
    from python_stdlib import is_stdlib_import
    source(db,path)
    for row in csv_rows(path):
        roots=json.loads(row['import_roots_json'])
        if row['manifest_event_count']=='0' and roots and all(is_stdlib_import(r) for r in roots):
            auxiliary(db,row['episode_id'],stdlib_only=True,stdlib_source_row=row)


def load_corrections(db, canonical, project):
    an=canonical/'analysis'
    old=project/'.Codex/plans/2026-08-15-rq2-six-language-hallucination-analysis'
    for name in ['v2_current_absence_official_requery.csv','v2_temporal_official_requery.csv']:
        requery(db,an/name)
    for path in [an/'v2_temporal_commit_metadata.csv',old/'six_language_hidden_postdate_commit_metadata.csv']:
        commit_times(db,path)
    maven_metadata(db,old/'six_language_maven_official_requery.csv')
    maven_times(db,old/'six_language_maven_artifact_times.csv')
    bound_scope(db,an/'six_language_candidate_evidence.csv')
    python_scope(db,an/'six_language_python_import_mapping.csv')
    source(db,an/'v2_native_adjudication.csv')
    for row in csv_rows(an/'v2_native_adjudication.csv'):
        db.execute('INSERT INTO old_labels VALUES(?,?)',(row['episode_id'],row['final_label']))
    db.commit()

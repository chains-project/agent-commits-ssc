"""Inventory a small Git export and separate data downloads without copying files."""
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

BASE=Path(__file__).resolve().parent
CORE={
    'inputs/observations.sqlite',
    'results_replay_v2/stage12.sqlite',
    'results_replay_v2/manifest_witnesses.jsonl.gz',
    'results_integrated/episodes.csv.gz',
    'results/episodes.csv.gz',
}
FIELDS=['path','bytes','MiB','sha256','purpose']


def destination(path):
    name=path.as_posix()
    if name in CORE:return 'drive_core','Current results, offline replay or full verification input'
    if name=='CLAUDE.md':return 'local_only','Local project instructions'
    if path.suffix=='.sqlite':return 'drive_optional','Earlier Java run or source-machine replay input'
    if name.endswith('.gz'):return 'drive_optional','Earlier row-level results or audit evidence'
    if name.startswith('research/java_high_frequency/trees/'):
        return 'drive_optional','Cached official source-tree responses for catalog reconstruction'
    if path.parts[0] in {'results_identity_audit','results_java_identity','results_java_identity_pilot'}:
        return 'drive_optional','Earlier development diagnostics'
    if name=='results_remaining_review/java_remaining_targets.csv':
        return 'drive_optional','Detailed unresolved-target inventory'
    return 'staging','Code, tests, small fixtures, report, summary or provenance'


def fingerprint(path):
    value=hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def inventory():
    groups={key:[] for key in ['staging','drive_core','drive_optional','local_only']}
    for path in sorted(BASE.rglob('*')):
        rel=path.relative_to(BASE)
        if not path.is_file() or any(p in {'__pycache__','demo_output','delivery'} for p in rel.parts):continue
        if path.suffix=='.pyc' or path.name=='package_manifest.json':continue
        if path.name.endswith(('.sqlite-wal','.sqlite-shm','.sqlite-journal')):continue
        target,purpose=destination(rel);size=path.stat().st_size
        groups[target].append(dict(path=rel.as_posix(),bytes=size,MiB=round(size/2**20,3),sha256=fingerprint(path),purpose=purpose))
    # A code-only checkout can inventory files before the external data is added.
    assert all(row['bytes']<50*2**20 for row in groups['staging'])
    return groups


def main():
    groups=inventory();output=BASE/'delivery';output.mkdir(exist_ok=True)
    for name,rows in groups.items():
        with (output/(name+'_files.csv')).open('w',newline='',encoding='utf-8') as handle:
            writer=csv.DictWriter(handle,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows)
    summary={name:dict(files=len(rows),bytes=sum(row['bytes'] for row in rows)) for name,rows in groups.items()}
    summary['destination']='https://github.com/chains-project/agent-commits-ssc/tree/<branch>/experiments/agent_dependency_study'
    summary['control_files']='Additionally commit delivery/ inventory CSVs, summary.json and main-table schema/verification JSON, plus package_manifest.json. These control files are excluded from their own inventory.'
    summary['excluded']='Python caches, SQLite WAL/SHM/journal files and demo outputs'
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()

"""UTC commit-date reconciliation and manifest-bound portable metadata loading."""
import csv
import gzip
import json
from datetime import timezone
from common import digest
from stage3_classify import timestamp


def normalize_date(value):
    stamp = timestamp(value)
    return stamp.astimezone(timezone.utc).isoformat().replace('+00:00','Z') if stamp else ''


def add_dates(item, author, committer, source):
    for field, value in [('author', author), ('committer', committer)]:
        normalized = normalize_date(value)
        if normalized:
            # Preserve one exact source locator for every distinct instant.
            item[field+'_candidates'].setdefault(normalized, source)


def resolve_dates(item):
    row = {k:item[k] for k in ['commit_id','repo','sha','raw_observations']}
    for field in ['author','committer']:
        candidates = item[field+'_candidates']
        row[field] = next(iter(candidates)) if len(candidates) == 1 else ''
        row[field+'_source'] = candidates.get(row[field], '')
        row[field+'_variants'] = json.dumps(candidates, sort_keys=True)
    row['metadata_status'] = 'complete' if row['committer'] else (
        'committer_conflict' if len(item['committer_candidates']) > 1 else 'committer_missing')
    return row


def load_dates(directory, stage12_sha=None):
    manifest = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    path = directory/'commit_times.csv.gz'
    assert digest(path) == manifest['output_sha256'], 'Commit-date supplement changed'
    if stage12_sha:
        assert manifest['stage12_sha256'] == stage12_sha, 'Commit-date population mismatch'
    with gzip.open(path,'rt',encoding='utf-8',newline='') as handle:
        rows = list(csv.DictReader(handle))
    result = {r['commit_id']:r for r in rows}
    assert len(result) == len(rows) == manifest['counts']['commits']
    return result, manifest

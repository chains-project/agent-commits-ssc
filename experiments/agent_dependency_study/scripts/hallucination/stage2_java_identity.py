"""Resolve documented Java import targets with commit-bound version context.

[IN]: Stage1 import, added manifest events, parsed Stage2 context, local catalog.
[OUT]: Identity/query context and complete mapping witness for catalog hits.
[SYNC]: java_class_catalog.json, stage2_episode_linker.py and identity pilot/tests.
"""
import json
from functools import lru_cache
from pathlib import Path, PurePosixPath
CATALOG_PATH = Path(__file__).with_name('java_class_catalog.json')


@lru_cache(maxsize=1)
def catalog():
    body = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
    targets = {}
    for rule in body['rules']:
        for target in rule['targets']:
            if target in targets:
                raise ValueError('Duplicate catalog target: ' + target)
            targets[target] = rule
    return targets


def rule_for(event):
    if event.get('ecosystem') == 'Maven' and event.get('event_type') == 'import':
        return catalog().get(event.get('raw_target', ''))
    return None


def ancestor_depth(source, manifest):
    if not manifest:
        return -1
    source_parts = PurePosixPath(source.replace('\\', '/')).parts[:-1]
    parent = PurePosixPath(manifest.replace('\\', '/')).parts[:-1]
    return len(parent) if source_parts[:len(parent)] == parent else -1


def declaration_rows(event, package, manifests, contexts):
    rows = []
    for item in manifests:
        if item['ecosystem'] == 'Maven' and item['package_candidate'] == package:
            rows.append(dict(path=item['path'], version=item['version_spec'], event=item))
    for item in contexts:
        path = item.get('dependency_path', '')
        if item.get('ecosystem') != 'Maven' or item.get('package_name') != package:
            continue
        if not path or item.get('source_path') != path:
            continue
        if item.get('alignment_status') in {'mapping_uncertain', 'first_party_excluded', 'private_or_local_excluded'}:
            continue
        rows.append(dict(path=path, version=item.get('resolved_version') or item.get('version_spec', ''), context=item))
    scoped = [(ancestor_depth(event['path'], r['path']), r) for r in rows]
    valid = [(depth, row) for depth, row in scoped if depth >= 0]
    deepest = max((depth for depth, _ in valid), default=-1)
    selected = [row for depth, row in valid if depth == deepest]
    return sorted(selected, key=lambda row: json.dumps(row, sort_keys=True))


def resolve_import(event, manifests, contexts, selected_context=None):
    rule = rule_for(event)
    if not rule:
        return None
    if selected_context and selected_context.get('alignment_status') in {'first_party_excluded', 'private_or_local_excluded'}:
        return None
    rows = declaration_rows(event, rule['coordinate'], manifests, contexts)
    versions = {r['version'].strip() for r in rows}
    unambiguous = len(versions) == 1
    version = next(iter(versions)) if unambiguous else ''
    reason = 'curated_java_identity|' + rule['id']
    if len(versions) > 1:
        reason += '|java_context_version_conflict'
    context = dict(package_name=rule['coordinate'], version_spec=version,
        resolved_version='', alignment_status='matched_existing_manifest' if rows else 'no_matching_manifest',
        resolution_source='documented_java_class', reason_codes=reason)
    witness = dict(rule_id=rule['id'], sources=rule['sources'], raw_target=event['raw_target'],
        coordinate=rule['coordinate'], source_path=event['path'], declarations=rows,
        version_basis='nearest_same_coordinate' if unambiguous else 'conflicting_declarations' if rows else 'name_only')
    return dict(context=context, manifests=[r['event'] for r in rows if 'event' in r] if unambiguous else [], witness=witness)

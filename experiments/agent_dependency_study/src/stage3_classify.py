"""One evidence-based decision tree for all episodes; old labels never enter here."""
from datetime import datetime, timezone
from functools import lru_cache
import re

PROXIES={'go_module_time','maven_index_timestamp','http_last_modified','legacy_unspecified_time'}


@lru_cache(maxsize=131072)
def timestamp(value):
    if not value:
        return None
    try:
        value=re.sub(r'\.(\d+)(?=Z$|[+-]\d{2}:\d{2}$)',
                     lambda m:'.'+(m.group(1)+'000000')[:6],value)
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        return result if result.tzinfo else None
    except (ValueError,TypeError):
        return None


def result(label, reason, evidence=(), **extra):
    return dict(label=label,subtype=reason if label=='hallucinated_dependency' else '',
                reason=reason,evidence_ids=sorted(set(evidence)),delta_seconds='',
                matching_version='',time_type='',proxy_time=False,**extra)


def scope_result(episode,aux):
    if episode['query_kind']=='excluded':
        return result('out_of_scope','upstream_explicit_exclusion')
    if aux.get('stdlib_only'):
        return result('out_of_scope','bound_stdlib_import')
    if aux.get('local_only') and episode['dependency_quadrant']=='I-M+':
        return result('out_of_scope','all_manifest_references_local')
    if episode['query_kind']=='unresolved' or not episode['package_name']:
        return result('insufficient_evidence','upstream_identity_unresolved')
    target=aux.get('effective_package',episode['package_name'])
    if episode['ecosystem']=='Maven' and ('${' in target or '${' in episode['query_value']):
        return result('insufficient_evidence','unresolved_property')
    return None


def classify(episode, observations, boundary, matcher, aux=None, exclude_proxies=False, prepared_facts=None):
    aux=aux or {}
    scoped=scope_result(episode,aux)
    if scoped:
        return scoped
    facts=prepared_facts if prepared_facts is not None else collect_facts(episode,observations,matcher,exclude_proxies)
    value=decide(facts,timestamp(boundary))
    package_negative=any(kind=='package' for kind,_ in facts['negative'])
    value['conflict_observed']=bool((facts['negative'] and facts['matches']) or
                                   (package_negative and facts['package_exists']))
    return value


def collect_facts(episode, observations, matcher, exclude_proxies):
    facts=dict(matches={},negative=[],obstacles=set(),uncertain_match=False,partial_match=False,enumerated_match=False,package_exists=False,considered=[])
    for oid,record in observations:
        if exclude_proxies and record.get('time_type') in PROXIES:
            record=dict(record,versions=[dict(r,published_at='') for r in record.get('versions',[])])
        observe(facts,episode,oid,record,matcher)
    return facts


def observe(facts,episode,oid,record,matcher):
    kind,query=episode['query_kind'],episode['query_value']
    if record.get('scope')=='exact' and record.get('exact_query') != query:
        return
    versions=record.get('versions',[])
    facts['considered'].append(oid)
    facts['package_exists'] |= bool(record['lookup_status']=='ok' and versions)
    valid,selected,unknown=matcher.match(episode['ecosystem'],kind,query,tuple(r['version'] for r in versions))
    if not valid:
        facts['obstacles'].add('unsupported_query_syntax')
        return
    if record['lookup_status']=='not_found':
        facts['negative'].append((record.get('negative_kind','package'),oid))
        return
    if record['lookup_status']!='ok':
        facts['obstacles'].add('registry_request_failed')
        return
    add_matches(facts,versions,selected,oid,record)
    facts['uncertain_match'] |= bool(unknown)
    facts['partial_match'] |= bool(selected and record.get('scope')=='observed_subset' and kind!='exact')
    facts['enumerated_match'] |= bool(selected and record.get('scope')=='enumeration')
    add_negative(facts,record,selected,unknown,oid)


def add_matches(facts,versions,selected,oid,record):
    for index in selected:
        row=versions[index]
        key=row['version']
        match=facts['matches'].setdefault(key,dict(times=[],evidence=set()))
        match['evidence'].add(oid)
        stamp=timestamp(row.get('published_at',''))
        if stamp:
            match['times'].append((stamp,record['time_type'],oid))


def add_negative(facts,record,selected,unknown,oid):
    if selected:
        return
    if unknown:
        facts['obstacles'].add('candidate_version_unparseable')
    elif record.get('scope')=='observed_subset':
        facts['obstacles'].add('legacy_omission_unknown')
    elif not record.get('versions'):
        facts['obstacles'].add('empty_version_history')
    else:
        facts['negative'].append(('version',oid))


def decide(facts,boundary):
    timed=[(stamp,version,kind,eid) for version,m in facts['matches'].items() for stamp,kind,eid in m['times']]
    if boundary and any(row[0]<=boundary for row in timed):
        return timed_result('present_at_commit','matching_release_before_boundary',min(timed),boundary)
    if facts['matches']:
        if not boundary:
            return result('insufficient_evidence','commit_time_missing',match_evidence(facts))
        if any(not v['times'] for v in facts['matches'].values()):
            return result('insufficient_evidence','matching_release_time_missing',match_evidence(facts))
        if facts['uncertain_match'] or (facts['partial_match'] and not facts['enumerated_match']):
            return result('insufficient_evidence','earliest_match_not_established',match_evidence(facts))
        return timed_result('hallucinated_dependency','postdate',min(timed),boundary)
    if facts['uncertain_match']:
        return result('insufficient_evidence','candidate_version_unparseable')
    if facts['negative']:
        if facts['package_exists'] and any(k=='version' for k,_ in facts['negative']):
            return result('hallucinated_dependency','version_no_match',facts['considered'])
        if not facts['package_exists'] and any(k=='package' for k,_ in facts['negative']):
            return result('hallucinated_dependency','package_no_match',facts['considered'])
        return result('insufficient_evidence','negative_scope_conflicts_with_package_knowledge',facts['considered'])
    reasons=sorted(facts['obstacles']) or ['registry_evidence_unavailable']
    return result('insufficient_evidence','|'.join(reasons),facts['considered'])


def match_evidence(facts):
    return sorted({eid for item in facts['matches'].values() for eid in item['evidence']})


def timed_result(label,reason,item,boundary):
    stamp,version,kind,eid=item
    value=result(label,reason,[eid])
    value.update(delta_seconds=int((stamp-boundary).total_seconds()),matching_version=version,
                 time_type=kind,proxy_time=kind in PROXIES,matching_time=stamp.isoformat())
    return value

"""Migrate applicable evidence and classify the rebuilt episode universe."""
from functools import lru_cache
import json
from common import unpack,pack
from stage3_run import evaluate_row
from stage3_classify import collect_facts
from stage3_matching import Matcher


def applicable_auxiliary(aux,old,episode,events=None):
    keys=('ecosystem','package_name','query_kind','query_value')
    if not old or any(old.get(k)!=episode.get(k) for k in keys):
        return {}
    result=dict(aux)
    if aux.get('stdlib_only') and not stdlib_witness_matches(aux,events):
        result.pop('stdlib_only',None)
    if aux.get('local_only') and not local_witness_matches(aux,events):
        result.pop('local_only',None)
    return result


def stdlib_witness_matches(aux,events):
    if not events or any(r['event_type']!='import' for r in events):return False
    roots={r['raw_target'].lstrip('.').split('.',1)[0] for r in events}
    known=set(json.loads(aux.get('stdlib_source_row',{}).get('stdlib_roots_json','[]')))
    return bool(roots) and roots<=known


def local_witness_matches(aux,events):
    keys=('event_id','path','raw_target','package_candidate','version_spec','event_status')
    signature=lambda row:tuple(row.get(k,'') for k in keys)
    expected={signature(r['event']) for r in aux.get('manifest_witnesses',[])}
    current={signature(r) for r in (events or []) if r['event_type']=='manifest_addition'}
    return bool(expected) and expected==current


class Evaluation:
    def __init__(self,evidence,metadata,node='node',stage=None):
        self.evidence,self.metadata,self.matcher=evidence,metadata,Matcher(node)
        self.stage=stage

    @lru_cache(maxsize=128)
    def episode_events(self,cid):
        if self.stage is None:return {}
        events={r['event_id']:r for r in unpack(self.stage.execute('SELECT body FROM events WHERE cid=?',(cid,)).fetchone()['body'])}
        by_episode={}
        for link in unpack(self.stage.execute('SELECT body FROM links WHERE cid=?',(cid,)).fetchone()['body']):
            by_episode.setdefault(link['episode_id'],[]).append(events[link['event_id']])
        return by_episode

    @lru_cache(maxsize=4096)
    def observations(self,eco,pkg):
        return [(r['id'],unpack(r['body'])) for r in self.evidence.execute(
            'SELECT id,body FROM observations WHERE eco=? AND pkg=? ORDER BY id',(eco,pkg))]

    @lru_cache(maxsize=1024)
    def facts(self,eco,pkg,kind,value,exclude):
        query=dict(ecosystem=eco,query_kind=kind,query_value=value)
        return collect_facts(query,self.observations(eco,pkg),self.matcher,exclude)

    def evaluate(self,row,boundary_mode='committer'):
        episode=unpack(row['body'])
        old=unpack(row['old_body']) if row['old_body'] else None
        original_aux=unpack(row['aux']) if row['aux'] else {}
        scoped=bool(original_aux.get('stdlib_only') or original_aux.get('local_only'))
        events=self.episode_events(row['cid']).get(row['id'],[]) if scoped else None
        aux=applicable_auxiliary(original_aux,old,episode,events)
        commit=self.metadata[row['cid']]
        source={**dict(row),**{k:commit[k] for k in ('repo','sha','author','committer','time_source')},'aux':pack(aux) if aux else None}
        value=evaluate_row(source,self.observations,self.matcher,self.facts,boundary_mode)
        author=evaluate_row(source,self.observations,self.matcher,self.facts,'author')
        for key in ('label','subtype','reason','boundary','delta_seconds'):
            value['author_'+key]=author[key]
        value.update(final_queryable=str(row['q']),baseline_queryable=str(row['old_q'] or 0),
            baseline_id_present=str(bool(old)),commit_id=row['cid'],
            historical_advisory_flag=str('historical_advisory_precedes_current_absence' in old.get('reason_codes','')) if old else 'not_relinked',
            auxiliary_scope_invalidated=str(any(original_aux.get(k) and not aux.get(k) for k in ('stdlib_only','local_only'))),
            commit_time_provenance=commit['time_source'])
        return value


def commit_metadata(stage,evidence,date_rows=None):
    saved={r['id']:dict(r) for r in evidence.execute('SELECT * FROM commits')}
    metadata={}
    for row in stage.execute('SELECT id,body FROM commits'):
        if row['id'] in saved:
            metadata[row['id']]=saved[row['id']]
        else:
            body=unpack(row['body'])
            metadata[row['id']]=dict(repo=body['repo'],sha=body['sha'],author=body['author_date'],
                committer='',time_source='new_materialized_commit_author_from_frozen_population')
    if date_rows is not None:
        assert set(metadata)==set(date_rows), 'Commit-date coverage does not match Stage1/2'
        for cid,item in metadata.items():
            dates=date_rows[cid]
            assert (item['repo'],item['sha'])==(dates['repo'],dates['sha']),cid
            item.update(author=dates['author'],committer=dates['committer'],
                        time_source=dates['committer_source'] or dates['metadata_status'])
    return metadata

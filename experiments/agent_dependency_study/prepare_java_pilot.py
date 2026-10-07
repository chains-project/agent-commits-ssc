"""Export 30 real Java commits and their saved evidence for a portable mapping pilot."""
import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
BASE = Path(__file__).resolve().parent
sys.path[:0] = [str(BASE/'src'), str(BASE/'scripts/hallucination'), str(BASE)]
from common import csv_rows, digest, read_db, unpack, write_json
from audit_insufficient import write_csv
from stage2_context import SqliteContextLookup
from stage2_java_identity import rule_for


def select_commits(source, per_library):
    pools, event_rules = defaultdict(set), {}
    for event in csv_rows(source/'events.csv'):
        rule = rule_for(event)
        if rule:
            pools[rule['coordinate']].add(event['commit_id'])
            event_rules[event['event_id']] = rule['coordinate']
    selected, rows = set(), []
    for library in sorted(pools):
        key = lambda cid: hashlib.sha256(('20261007|'+cid).encode()).hexdigest()
        for cid in sorted(pools[library]-selected, key=key)[:per_library]:
            selected.add(cid)
            rows.append(dict(commit_id=cid, selection_library=library))
    return selected, rows, event_rules


def export_tables(source, output, selected, event_rules):
    commits = [r for r in csv_rows(source/'commits.csv') if r['commit_id'] in selected]
    episodes = [r for r in csv_rows(source/'episodes.csv') if r['commit_id'] in selected]
    eids = {r['episode_id'] for r in episodes}
    events = [r for r in csv_rows(source/'events.csv') if r['commit_id'] in selected]
    links, opportunities = [], defaultdict(set)
    unresolved = {r['episode_id'] for r in csv_rows(source/'episodes.csv') if r['query_kind']=='unresolved'}
    for row in csv_rows(source/'episode_event_links.csv'):
        if row['episode_id'] in eids:
            links.append(row)
        if row['episode_id'] in unresolved and row['event_id'] in event_rules:
            opportunities[event_rules[row['event_id']]].add(row['episode_id'])
    for name, data in [('commits',commits),('events',events),('old_episodes',episodes),('old_links',links)]:
        write_csv(output/(name+'.csv'), data)
    return commits, episodes, {k:len(v) for k,v in opportunities.items()}


def export_evidence(output, commits, episodes, context):
    lookup = SqliteContextLookup(context)
    contexts = [row for commit in commits for row in lookup.lookup(commit)]
    lookup.close()
    write_json(output/'contexts.json', contexts)
    db = read_db(BASE/'inputs/observations.sqlite')
    times, aux, observations = {}, {}, []
    for commit in commits:
        times[commit['commit_id']] = dict(db.execute('SELECT * FROM commits WHERE id=?',(commit['commit_id'],)).fetchone())
    for episode in episodes:
        row = db.execute('SELECT body FROM auxiliary WHERE eid=?',(episode['episode_id'],)).fetchone()
        if row:
            aux[episode['episode_id']] = unpack(row['body'])
    packages = {r['package_name'] for r in episodes}
    packages.update(r.get('effective_package','') for r in aux.values())
    packages.update({'org.projectlombok:lombok','org.slf4j:slf4j-api','org.junit.jupiter:junit-jupiter-api'})
    for package in sorted(packages):
        for row in db.execute("SELECT id,body FROM observations WHERE eco='Maven' AND pkg=? ORDER BY id",(package,)):
            observations.append(dict(id=row['id'], record=unpack(row['body'])))
    write_json(output/'commit_times.json', times)
    write_json(output/'auxiliary.json', aux)
    write_json(output/'observations.json', observations)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ['source','context','output']:
        parser.add_argument('--'+arg,type=Path,required=True)
    parser.add_argument('--per-library',type=int,default=10)
    args = parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    selected, selection, rules = select_commits(args.source,args.per_library)
    write_csv(args.output/'selection.csv',selection)
    commits, episodes, opportunities = export_tables(args.source,args.output,selected,rules)
    export_evidence(args.output,commits,episodes,args.context)
    sources = [args.source/name for name in ['commits.csv','events.csv','episodes.csv','episode_event_links.csv']]
    sources += [args.context,BASE/'inputs/observations.sqlite',BASE/'scripts/hallucination/java_class_catalog.json']
    write_json(args.output/'manifest.json',dict(commits=len(commits),old_episodes=len(episodes),
        selection='SHA256(20261007|commit_id) ascending; 10 unused commits per coordinate in sorted coordinate order',
        catalog_target_opportunities_in_all_java_unresolved=opportunities,
        sources=[dict(path=str(p),sha256=digest(p)) for p in sources],
        inputs={p.name:digest(p) for p in sorted(args.output.iterdir()) if p.is_file()}))
    print(json.dumps(dict(commits=len(commits),old_episodes=len(episodes),opportunities=opportunities)))


if __name__=='__main__':
    main()

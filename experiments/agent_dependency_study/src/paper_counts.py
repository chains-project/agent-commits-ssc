"""Episode-level stratified counts with complete attribution and repository unions."""
import csv
from collections import Counter, defaultdict

CURRENT = ('present_at_commit', 'hallucinated_dependency', 'insufficient_evidence', 'out_of_scope')
HISTORICAL = ('confirmed_hallucination', 'probable_hallucination', 'indeterminate',
              'not_hallucination', 'not_in_candidate_review')


class Counts:
    def __init__(self, version, labels):
        self.version, self.labels = version, labels
        self.counts = defaultdict(Counter)
        self.repos = defaultdict(set)
        self.commits = defaultdict(set)
        self.overlap = defaultdict(Counter)

    def add(self, cid, eco, q, label, member, subtype=''):
        agents = member['agents'].split('|')
        assert agents == sorted(set(agents)) and all(agents)
        assert label in self.labels
        groups = [('global', 'GLOBAL', 'ALL'), ('ecosystem', 'ALL', eco)]
        groups += [('agent', a, 'ALL') for a in agents]
        groups += [('agent_ecosystem', a, eco) for a in agents]
        for key in groups:
            self.counts[key].update({'episodes': 1, 'queryable': q, label: 1, 'q_'+label: q})
            if q:
                self.repos[key].add(member['repository_key'])
                self.commits[key].add(cid)
            if subtype:
                self.counts[key][subtype] += 1
        if len(agents) > 1:
            self.overlap['|'.join(agents)].update({'episodes': 1, 'queryable': q, label: 1})

    def records(self, dimension):
        result = []
        for key, count in sorted(self.counts.items()):
            if key[0] != dimension:
                continue
            row = dict(version=self.version, agent=key[1], ecosystem=key[2],
                       episodes=count['episodes'], queryable=count['queryable'],
                       queryable_commits=len(self.commits[key]), queryable_repositories=len(self.repos[key]))
            for label in self.labels:
                row[label] = count[label]
                row['queryable_'+label] = count['q_'+label]
                row['queryable_'+label+'_pct'] = round(100*count['q_'+label]/count['queryable'], 6) if count['queryable'] else ''
            if self.labels == CURRENT:
                row.update({k:count[k] for k in ['package_no_match', 'version_no_match', 'postdate']})
            result.append(row)
        return result

    def verify(self):
        global_row = self.counts['global', 'GLOBAL', 'ALL']
        assert global_row['episodes'] == sum(global_row[x] for x in self.labels)
        assert global_row['queryable'] == sum(global_row['q_'+x] for x in self.labels)
        for field in ['episodes', 'queryable', *self.labels]:
            assert global_row[field] == sum(c[field] for k,c in self.counts.items() if k[0] == 'ecosystem')
            agent_sum = sum(c[field] for k,c in self.counts.items() if k[0] == 'agent')
            extra = sum((len(a.split('|'))-1)*c[field] for a,c in self.overlap.items())
            assert agent_sum == global_row[field] + extra
        for key, count in self.counts.items():
            if key[0] == 'agent':
                combined = sum((c for k,c in self.counts.items() if k[:2] == ('agent_ecosystem',key[1])), Counter())
                assert all(count[k] == combined[k] for k in set(count) | set(combined))

    def save(self, output, prefix):
        self.verify()
        for dim in ['global', 'agent', 'ecosystem', 'agent_ecosystem']:
            save_csv(output/(prefix+'_'+dim+'.csv'), self.records(dim))
        overlap = [dict(version=self.version, agents=a, **{k:c[k] for k in ['episodes','queryable',*self.labels]})
                   for a,c in sorted(self.overlap.items())]
        save_csv(output/(prefix+'_overlap.csv'), overlap)


def save_csv(path, rows):
    assert rows
    with path.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

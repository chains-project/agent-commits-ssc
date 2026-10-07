"""Inventory saved release evidence; does not define a freshness estimand."""
from collections import Counter, defaultdict
from functools import lru_cache
from common import unpack
from stage3_classify import timestamp, PROXIES
from paper_counts import save_csv


class Freshness:
    def __init__(self, db):
        self.db = db
        self.counts = defaultdict(Counter)

    @lru_cache(maxsize=2048)
    def history(self, eco, package):
        records = [unpack(r['body']) for r in self.db.execute(
            'SELECT body FROM observations WHERE eco=? AND pkg=?', (eco,package))]
        enumerations = [r for r in records if r['scope'] == 'enumeration'
                        and r['lookup_status'] == 'ok' and r['versions']]
        dated = [r for r in enumerations if all(timestamp(v.get('published_at')) for v in r['versions'])]
        return enumerations, dated

    def add(self, row):
        if row['final_queryable'] != '1':
            return
        enumerations, dated = self.history(row['ecosystem'], row['effective_package'])
        metrics = dict(queryable=1, with_nonempty_enumeration=int(bool(enumerations)),
                       with_fully_dated_enumeration=int(bool(dated)))
        if row['query_kind'] == 'exact':
            known = bool(timestamp(row.get('matching_time')))
            before = row['label'] == 'present_at_commit'
            selected = row['matching_version']
            included = any(any(v['version'] == selected for v in r['versions']) for r in dated)
            metrics.update(exact=1, exact_with_selected_time=int(known),
                           exact_present_at_commit=int(before),
                           exact_present_with_nonproxy_time=int(before and row['time_type'] not in PROXIES),
                           exact_with_enumeration=int(bool(enumerations)),
                           exact_with_fully_dated_enumeration=int(bool(dated)),
                           exact_present_in_fully_dated_enumeration=int(before and included))
        for eco in ['GLOBAL', row['ecosystem']]:
            self.counts[eco].update(metrics)

    def save(self, output):
        fields = sorted(set().union(*(set(c) for c in self.counts.values())))
        rows = [dict(ecosystem=eco, **{k:c[k] for k in fields}) for eco,c in sorted(self.counts.items())]
        save_csv(output/'freshness_evidence_coverage.csv', rows)

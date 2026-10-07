"""Aggregate positive release-to-committer gaps without changing labels."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

LIMITS = (7200, 86400, 604800, 2592000)
INTERVALS = ('0–2 hours', '>2–24 hours', '>24 hours–7 days', '>7–30 days', '>30 days')


def interval_index(value):
    try:
        delta = Decimal(value)
    except InvalidOperation as error:
        raise ValueError('Postdate time difference is missing or invalid') from error
    if not delta.is_finite() or delta <= 0:
        raise ValueError('Postdate must have a finite positive time difference')
    return next((i for i, upper in enumerate(LIMITS) if delta <= upper), 4)


class PostdateCounts:
    def __init__(self):
        self.counts = defaultdict(Counter)
        self.proxies = defaultdict(Counter)
        self.seen = set()

    def add(self, row):
        if row['label'] != 'hallucinated_dependency' or row['subtype'] != 'postdate':
            return
        assert row['boundary_source'] == 'committer'
        assert row['final_queryable'] == '1'
        assert row['episode_id'] not in self.seen
        self.seen.add(row['episode_id'])
        index = interval_index(row['delta_seconds'])
        for ecosystem in ('GLOBAL', row['ecosystem']):
            self.counts[ecosystem][index] += 1
            self.proxies[ecosystem][index] += row['proxy_time'].lower() in ('true', '1')

    def records(self):
        result = []
        for ecosystem, counts in sorted(self.counts.items()):
            total, cumulative = sum(counts.values()), 0
            for index, interval in enumerate(INTERVALS):
                cumulative += counts[index]
                result.append(dict(ecosystem=ecosystem, interval=interval,
                    lower_exclusive_seconds=0 if index == 0 else LIMITS[index-1],
                    upper_inclusive_seconds=LIMITS[index] if index < 4 else '',
                    episodes=counts[index], denominator_postdate=total,
                    pct_of_postdate=round(100*counts[index]/total, 6),
                    cumulative_episodes=cumulative if index < 4 else '',
                    cumulative_pct_of_postdate=round(100*cumulative/total, 6) if index < 4 else '',
                    proxy_episodes=self.proxies[ecosystem][index]))
        return result


def report_section(records):
    rows = [row for row in records if row['ecosystem'] == 'GLOBAL']
    total = int(rows[0]['denominator_postdate'])
    table = ['| Release time after commit | Episodes | Share of postdate |', '| --- | ---: | ---: |']
    table += [f"| {r['interval']} | {int(r['episodes']):,} | {float(r['pct_of_postdate']):.2f}% |" for r in rows]
    cumulative = '; '.join(f"within {label}: {int(row['cumulative_episodes']):,} ({float(row['cumulative_pct_of_postdate']):.2f}%)"
                          for row, label in zip(rows, ('2 hours', '24 hours', '7 days', '30 days')))
    return (f'For the {total:,} `postdate` episodes, the interval is the recorded matching-release time '
            'minus committer time. The groups below do not overlap; upper boundaries are inclusive. '
            'Percentages use all postdate episodes as the denominator and retain the main analysis\'s timestamp proxies.\n\n'
            + '\n'.join(table) + '\n\nCumulative coverage: ' + cumulative + '. '
            'These intervals describe the time differences and do not change the final labels.\n')

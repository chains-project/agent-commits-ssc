"""[IN] Final episode gzip. [OUT] Interval CSV/provenance. [POS] Offline summary."""
import argparse
import csv
import gzip
import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE/'src'))
from postdate_summary import PostdateCounts


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(4*1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--episodes', type=Path, default=BASE/'results_integrated/episodes.csv.gz')
    parser.add_argument('--output', type=Path, default=BASE/'results_integrated')
    args = parser.parse_args()
    summary = json.loads((args.episodes.parent/'summary.json').read_text(encoding='utf-8'))
    digest = sha(args.episodes)
    assert digest == summary['output_sha256']['episodes.csv.gz']
    counts, total = PostdateCounts(), 0
    with gzip.open(args.episodes, 'rt', encoding='utf-8', newline='') as handle:
        for row in csv.DictReader(handle):
            counts.add(row)
            total += 1
    assert total == summary['episodes']
    with (args.episodes.parent/'label_counts.csv').open(encoding='utf-8') as handle:
        expected = sum(int(r['episodes']) for r in csv.DictReader(handle)
                       if r['stratum'] == 'global' and r['subtype'] == 'postdate')
    assert len(counts.seen) == expected
    rows = counts.records()
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output/'postdate_intervals.csv'
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    receipt = dict(result_version=summary['result_version'], input_sha256=digest,
        episodes_scanned=total, postdate_episodes=expected, boundary='committer',
        denominator='all global postdate episodes; per-ecosystem postdate episodes for ecosystem rows',
        intervals='positive gap; upper limits inclusive; bins mutually exclusive',
        proxies_included=True, output_sha256=sha(path), label_changes=False, network=False)
    (args.output/'postdate_intervals.json').write_text(json.dumps(receipt, indent=2)+'\n', encoding='utf-8')
    print(json.dumps([r for r in rows if r['ecosystem'] == 'GLOBAL']), flush=True)


if __name__ == '__main__':
    main()

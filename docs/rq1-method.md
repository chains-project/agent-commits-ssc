# RQ1 method

RQ1 builds a multi-agent GitHub commit corpus. Collection scripts search agent-specific channels in bounded UTC windows, record query segments, retry incomplete or capped segments, and retain the source channel for provenance.

Processing scripts normalize rows, remove invalid records, deduplicate by repository and commit SHA, and merge the corrected Claude and non-Claude populations. Metadata scripts add repository attributes, commit JSON, changed file information, diffs, and language assignments.

## Inputs and outputs

Primary inputs are GitHub Search/API responses and the channel definitions in `scripts/collection`. Stable compact outputs in this repository are under `results/summary/rq1`. Full populations, repository metadata, raw JSON, and diff corpora are external data products.

## Reproduction boundary

Collection requires `GITHUB_TOKEN` and is rate-limit dependent. `THESIS_DATA_ROOT` or explicit CLI paths should point to an external data directory. The initial public release does not rerun RQ1 collection and does not include display figures. RQ1 summaries remain preliminary because the final validity closure is not part of this release.

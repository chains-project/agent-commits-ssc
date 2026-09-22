# Data availability

## Included in Git

The repository includes compact RQ1 summaries, RQ2 summary JSON/CSV,
machine-readable validation records, the canonical table index, and the
approximately 34 MB, 74,618-row final adjudication CSV.

The exploratory attribution-feasibility supplement includes final signature
and discovery counts, two reduced 120-case tables, and a descriptive summary.
The tables retain frozen sample-design fields and consensus labels, with
declared model tokens, evidence-source categories and diff-availability
statuses. They support exact replay of sample counts, not reassessment of
coding decisions or verification of model execution. Complete messages,
patches, source locators and coding rationales remain external. No population
rate, conditional model composition or interval table is included in this
feasibility package.

## External packages

The following data remain external:

| Package | Contents | Size | URL | License or permission |
|---|---|---:|---|---|
| RQ2 canonical v2 | Six languages, four tables per language | See canonical_table_index.json | PENDING | Redistribution review pending |
| RQ1 populations | Corrected, merged, and non-Claude populations | See publication manifest | PENDING | Redistribution review pending |
| Repository metadata | Repository, queue, and raw metadata | See publication manifest | PENDING | Redistribution review pending |
| Diff corpus | Commit JSON and unified patches | Multi-GB | PENDING | Redistribution review pending |
| Pilot collection | Raw queries, segments, and JSONL outputs | PENDING | PENDING | Redistribution review pending |
| Model-observability scan inputs | Frozen population, message shards, diff index, input manifest, baseline closure record, and complete bounded coding inputs | Multi-GB | Not published | Separately held; source permission and redistribution review required |

data/rq2/canonical_table_index.json records each canonical table's byte size,
row count, SHA-256, fields, and primary key. The external package should retain
the indexed relative language/table layout. Download URLs and bundle SHA-256
values will be added only after a separate upload authorization.

## Data-license boundary

The MIT License applies to original repository software and documentation. It
does not automatically grant rights over third-party GitHub content, registry
records, or external datasets. Public repository identifiers and commit SHAs
are retained for reproducibility; tokens, local absolute paths, raw review
packets, and internal orchestration files are excluded.

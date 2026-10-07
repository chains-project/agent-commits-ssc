# Data availability

## Included in Git

The repository includes compact RQ1 summaries, RQ2 summary JSON/CSV,
machine-readable validation records, the canonical table index, and the
approximately 34 MB, 74,618-row final adjudication CSV.

## External packages

The following data remain external:

| Package | Contents | Size | URL | License or permission |
|---|---|---:|---|---|
| RQ2 canonical v2 | Six languages, four tables per language | See canonical_table_index.json | PENDING | Redistribution review pending |
| RQ1 populations | Corrected, merged, and non-Claude populations | See publication manifest | PENDING | Redistribution review pending |
| Repository metadata | Repository, queue, and raw metadata | See publication manifest | PENDING | Redistribution review pending |
| Diff corpus | Commit JSON and unified patches | Multi-GB | PENDING | Redistribution review pending |
| Pilot collection | Raw queries, segments, and JSONL outputs | PENDING | PENDING | Redistribution review pending |

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


## Integrated dependency study, 7 October 2026

The current `canonical_v3` update uses recovered committer dates for all 343,790 commits. Author time is a separate sensitivity comparison; episode identities and queryable denominators are unchanged.

The [integrated study](../experiments/agent_dependency_study/README.md) contains
the complete code workflow, tests, compact results and the current
[report](../experiments/agent_dependency_study/REPORT.md). Its operational
four-label analysis covers 1,679,785 episodes and 1,335,133 queryable episodes.
Earlier canonical Confirmed/Probable results remain a separate historical product.
Large inputs and the four structural CSV tables are distributed separately;
see the [data-access guide](../experiments/agent_dependency_study/DATA_ACCESS.md) and
file hashes under `experiments/agent_dependency_study/delivery/`.
Download this version from [Google Drive](https://drive.google.com/drive/folders/1Qp-xgDnHjzgSQWFrJgoIo_5FvHnEq7Ta?usp=sharing).

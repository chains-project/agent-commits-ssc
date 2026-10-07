# Legacy Stage3 offline evidence input contract

Recorded interface: 3 August 2026. This document describes the retained
`stage3_apply.py` interface. The integrated four-label analysis is described in
`../../REPORT.md` and uses the current standalone classification entry.

## Purpose

`stage3_apply.py` reads fixed local JSONL evidence, interprets registry and
advisory states at each commit's author time, and updates `episodes.csv`.
Network retrieval belongs to the collector. Retrieval failures remain explicit.

## Common fields

Each JSON object includes `evidence_type` (`registry` or `advisory`), `source`,
`ecosystem`, normalized `package_name`, `lookup_status`, and a timezone-aware
ISO 8601 `queried_at`. An optional `error` contains a short diagnostic without
credentials or request headers.

## Registry snapshots

Query key: `registry|ecosystem|package_name`. A package snapshot can serve multiple
requests and commits; the legacy apply stage interprets each at its author time.

```json
{"evidence_type":"registry","source":"npm","ecosystem":"npm","package_name":"left-pad","lookup_status":"ok","queried_at":"2026-08-03T00:00:00Z","versions":[{"version":"1.3.0","published_at":"2018-04-09T01:10:45Z"}]}
```

- `ok`: `versions` is a list of `version` and optional `published_at` fields.
- `not_found`: a current package-absence observation; the legacy stage emits `current_absent_unknown`.
- Retrieval errors or rate limits produce `cannot_compare` with a reason.
- Missing release times do not establish commit-time availability.
- npm/Cargo ranges normally exclude prereleases unless explicitly requested.
- Legacy Maven interval ordering supports numeric intervals; unsupported qualifier comparisons remain explicit.

## Advisory evidence

Query key: `advisory|ecosystem|package_name|query_kind|query_value`.

```json
{"evidence_type":"advisory","source":"OSV","ecosystem":"npm","package_name":"left-pad","query_kind":"exact","query_value":"1.3.0","lookup_status":"ok","queried_at":"2026-08-03T00:00:00Z","advisories":[{"id":"OSV-EXAMPLE","published":"2020-01-01T00:00:00Z","withdrawn":"","hydration_status":"ok","is_malicious":false}]}
```

`query_kind` is exact, name_only or range, and `advisories` is a list. Complete
records use `published` and `withdrawn`; `modified` is not a substitute.
`hydration_status` defaults to `ok`. Incomplete hydration cannot produce
`none_observed`. Malicious flags are retained in reasons/evidence, as
`is_malicious` or `database_specific.malicious`, rather than a new advisory status.

- `ok`: retrieval and hydration completed; an empty list means `none_observed`.
- `hydration_incomplete`: at least one required full record is missing.
- `unsupported`: the query has no supported advisory method and remains `not_queried`.
- `error`: failed retrieval remains incomplete rather than a negative advisory result.

## Completeness and storage

Every queryable legacy episode requires both registry and advisory records before
formal application. Conflicting payloads for the same query key are rejected.
Payloads are canonicalized as compact, sorted UTF-8 JSON and hashed with SHA-256.
Distinct payloads are stored under `stage3_evidence_cache/sha256/`; `evidence.jsonl`
contains IDs, keys, source/status/time fields, payload hash/path and diagnostics.

## Run

```bash
python scripts/hallucination/stage3_apply.py --input-dir staged_tables --evidence-input fixed_stage3_evidence.jsonl
```

The input contains the four Stage1/2 CSVs and schema manifest. Queryable episodes
must still have Stage2 `not_queried` states. Existing Stage3 outputs are rejected.
The runner prepares results in a hidden staging directory and restores the prior
episode table/schema if replacement fails.

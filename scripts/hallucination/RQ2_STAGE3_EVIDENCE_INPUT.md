# RQ2 Stage 3 Offline Evidence Input Contract

Date: 2026-08-03

## 1. Purpose and boundary

`rq2_stage3_apply.py` does not access live registries or OSV. It reads fixed local JSONL evidence, interprets registry and advisory states against each commit's `author_date`, and writes the resulting states back to `episodes.csv`.

This contract supports network collectors, hand-built fixtures, and offline regression tests. Current registry state must not be treated as a complete historical record, and a failed advisory lookup must not be interpreted as evidence that no advisory exists.

## 2. Common fields

Each line must contain one JSON object with these fields:

- `evidence_type`: `registry` or `advisory`.
- `source`: evidence source, such as `npm`, `PyPI`, `crates.io`, `Maven Central`, or `OSV`.
- `ecosystem`: the episode ecosystem, such as `npm`, `PyPI`, `Cargo`, `Go`, or `Maven`.
- `package_name`: the package name normalized by Stage 2.
- `lookup_status`: retrieval status for this offline evidence record.
- `queried_at`: timezone-aware ISO 8601 timestamp.
- `error`: optional short failure description; it must not include tokens, cookies, or request headers.

## 3. Registry package snapshot

The registry-record query key is:

`registry|ecosystem|package_name`

One package snapshot may support several query kinds and commits, but each episode must interpret it against its own `author_date`.

```json
{"evidence_type":"registry","source":"npm","ecosystem":"npm","package_name":"left-pad","lookup_status":"ok","queried_at":"2026-08-03T00:00:00Z","versions":[{"version":"1.3.0","published_at":"2018-04-09T01:10:45Z"}]}
```

Recommended `lookup_status` values:

- `ok`: `versions` is a list whose entries contain `version` and optional `published_at`.
- `not_found`: the package is absent from the current lookup; Stage 3 may only assign `current_absent_unknown`.
- `error`, `rate_limited`, or `temporary_error`: Stage 3 assigns `cannot_compare` and retains the specific reason.

Missing `published_at` values must not be used to infer commit-time existence. npm and Cargo ranges exclude prereleases unless the range explicitly includes them. Maven support is limited to numeric intervals; qualified versions are unsupported and remain non-comparable rather than being ordered with PEP 440 rules.

## 4. Advisory query evidence

The advisory-record query key is:

`advisory|ecosystem|package_name|query_kind|query_value`

```json
{"evidence_type":"advisory","source":"OSV","ecosystem":"npm","package_name":"left-pad","query_kind":"exact","query_value":"1.3.0","lookup_status":"ok","queried_at":"2026-08-03T00:00:00Z","advisories":[{"id":"OSV-EXAMPLE","published":"2020-01-01T00:00:00Z","withdrawn":"","hydration_status":"ok","is_malicious":false}]}
```

Constraints:

- `query_kind` must be `exact`, `name_only`, or `range`.
- `advisories` must be a list.
- Complete records use `published` and `withdrawn`; `modified` is not a lifecycle substitute.
- `hydration_status` defaults to `ok`; incomplete hydration must not produce `none_observed`.
- Malicious-package evidence uses `is_malicious=true` or `database_specific.malicious=true`. It remains evidence and does not create a new advisory status.

Recommended `lookup_status` values:

- `ok`: query and hydration completed; only an empty list may be interpreted as `none_observed`.
- `hydration_incomplete`: at least one complete record could not be retrieved.
- `unsupported`: the query kind has no reliable advisory lookup, so its state remains `not_queried`.
- `error`: the base query failed; mark hydration incomplete and do not claim that no advisory exists.

## 5. Integrity and deduplication

- Every exact, name-only, and range episode must have both registry and advisory records before a formal write.
- Conflicting payloads for the same query key cause the run to fail.
- Input records are normalized as sorted, compact UTF-8 JSON and hashed with SHA-256.
- Complete payloads are stored at `stage3_evidence_cache/sha256/<first-two>/<sha256>.json`; identical payloads are stored once.
- `evidence.jsonl` stores only the evidence ID, query key, source, status, timestamp, payload hash/path, and error summary.

## 6. Execution

```powershell
C:\ProgramData\Anaconda3\python.exe scripts\hallucination\rq2_stage3_apply.py `
  --input-dir data_products\rq2_stage1_v2 `
  --evidence-input C:\path\to\fixed_stage3_evidence.jsonl
```

Requirements:

- The input directory already contains the four Stage 1-2 CSV tables and `schema_manifest.json`.
- Queryable episodes remain in the Stage 2 state with both evidence axes set to `not_queried`.
- The output directory does not contain prior Stage 3 evidence, caches, summaries, or reports.

The runner prepares all content in a hidden staging directory. If the manifest update fails after replacing `episodes.csv`, it restores the original episode table and schema manifest. A successful run leaves no backup CSV files.

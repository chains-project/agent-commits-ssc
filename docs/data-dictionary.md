# Data dictionary

## Final adjudication CSV

| Field | Meaning |
|---|---|
| case_id | Stable adjudication-case identifier |
| episode_id | Stable dependency-episode identifier |
| languages_json | Changed-language list encoded as JSON |
| repo | GitHub repository identifier |
| sha | Commit SHA |
| agent | Agent attribution channel |
| ecosystem | Package ecosystem |
| package_name | Normalized dependency name |
| query_kind | Registry query semantics, such as exact, range, or name-only |
| query_value | Normalized version or direct-reference value |
| original_registry_status | Stage 3 registry conclusion before adjudication |
| final_label | Conservative four-class adjudication label |
| confidence | Confidence category assigned by the adjudicator |
| adjudication_basis | Concise basis for the final decision |
| evidence_flags_json | Machine-readable supporting or limiting flags |
| patch_path | Path beneath the separately distributed diff corpus |

## Final labels

- confirmed_hallucination: available evidence satisfies the strongest
  operational hallucination rule.
- probable_hallucination: evidence is strong but does not satisfy every
  confirmed requirement.
- not_hallucination: local, historical, mapping, version, or comment evidence
  explains the dependency.
- indeterminate: available evidence cannot support a stronger conclusion.

## Canonical Stage 1-3 tables

The external canonical package contains commits.csv, events.csv, episodes.csv,
and episode_event_links.csv for each of six languages. Exact fields, primary
keys, row counts, sizes, and SHA-256 values are stored in
data/rq2/canonical_table_index.json.

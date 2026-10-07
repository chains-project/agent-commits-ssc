# Supplementary paper tables

These tables summarize existing results. No labels, episode definitions or registry evidence were changed.

`historical_*` uses canonical v2: 1,364,856 queryable episodes, 322 Confirmed and 382 Probable. `current_*` uses `canonical_v3`: 1,335,133 queryable episodes and the four categories in `REPORT.md`.

- `*_global.csv`, `*_agent.csv`, `*_ecosystem.csv`: counts and shares, plus distinct queryable commits and repositories.
- `*_agent_ecosystem.csv`: the full agent-by-ecosystem table for each version.
- `*_overlap.csv`: multi-agent episode counts by agent combination.
- `historical_positive_time_counts.csv` and `historical_positive_time_evidence.csv`: final temporal boundaries and supporting records for the historical positives.
- `freshness_evidence_coverage.csv`: current release-evidence coverage, globally and by ecosystem.

An episode counts once globally and once in each qualifying agent. Channels do not multiply episodes. Repository counts use the union of GitHub numeric IDs among queryable episodes, with an explicit name fallback where metadata is unavailable. Repositories can appear in several strata.

Columns prefixed `queryable_` restrict labels to exact/range/name-only episodes. Percentage columns divide by that row's queryable denominator. Other label columns include all episodes. Historical `not_in_candidate_review` is separate from `not_hallucination`.

The freshness columns count available evidence. `with_nonempty_enumeration` means at least one successful saved record with enumeration scope and a nonempty version list. `with_fully_dated_enumeration` additionally requires every listed version to have a usable date. Exact-version columns restrict this to exact requests. `exact_present_in_fully_dated_enumeration` also requires the selected version to occur in that list and be dated by the commit. This does not establish that the registry retained every historical release or decide which prereleases belong in a freshness measure. Non-proxy selected times exclude Maven index/Last-Modified, Go module time and unspecified legacy timestamps.

## Reproduce

From the experiment directory, with the current result CSV and observation database downloaded:

```bash
python summarize_paper_tables.py --output reproduced_paper_tables
python -m unittest discover -s tests -p 'test_paper_*.py' -v
```

The small `inputs/analysis_membership/commits.csv.gz` file is included in Git. It reuses the previously checked complete membership relation and frozen repository metadata. `PROVENANCE.json` binds the inputs and checks totals, stratum margins and overlap counts. Historical agent counts agree with the original canonical summary tables.

The historical time check uses the original local evidence files:

```bash
python summarize_historical_time_basis.py --canonical-analysis PATH_TO_CANONICAL/analysis --historical-evidence PATH_TO_HISTORICAL_EVIDENCE --output reproduced_paper_tables
```

Exact sources are recorded in `HISTORICAL_TIME_PROVENANCE.json`; the compact supporting evidence is included in the CSV. This check recomputes recorded time differences and compares the boundary to the recovered raw committer date. It does not re-adjudicate the cases.

`python verify_paper_tables.py` checks the supplied tables against independently queried repository unions and the original canonical margins. Four targeted tests cover agent overlap, repository unions and partial release histories.

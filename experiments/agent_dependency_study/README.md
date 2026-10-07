# Dependency-registry experiment: complete code workflow

This folder accompanies the paper's dependency study. It contains the collection, corpus construction, extraction/alignment and registry-processing source chain, plus the unified classifier introduced on 7 October 2026.

Raw corpus: [ASSERT-KTH/agent-commits-raw](https://huggingface.co/datasets/ASSERT-KTH/agent-commits-raw). Import the required files from that dataset to run the extraction workflow.

Data and reproduction: [DATA_ACCESS.md](DATA_ACCESS.md) identifies the latest experiment version, data-download status, required file layout, four structural tables, final labels and offline commands. The results-only and optional reproduction bundles contain large data files; code and reports are kept in Git. [Download the data from Google Drive](https://drive.google.com/drive/folders/1Qp-xgDnHjzgSQWFrJgoIo_5FvHnEq7Ta?usp=sharing).

The results-only download contains `tables_stage12/commits.csv.gz`, `events.csv.gz`, `episodes.csv.gz` and `episode_event_links.csv.gz`, exported from the verified Stage1/2 database. Their schema and row counts are in `tables_stage12/manifest.json` (both the runtime manifest and `delivery/main_tables_manifest.json` are committed). Final classification is in the separate `results_integrated/episodes.csv.gz`; join it by `episode_id`. The Stage1/2 `agent` field preserves its upstream stored value and is not the complete multi-agent membership relation.

## Start here

Python and Node.js are prerequisites; this run used Python 3.9.12 and Node.js 23.7.0. npm semver 7.6.3, packaging 21.3 and pyparsing 3.0.4 are included under `vendor/`; no `pip install`, `npm install`, credential or network is required for the offline commands. Use `--node /path/to/node` when Node is not on PATH. Run commands from this directory.

```bash
python -m unittest discover -s tests -v
python run_integrated.py --reuse-stage12 results_replay_v2 --output reproduced --update-report
```

After adding both data bundles, the Stage1/2 database supplies the rebuilt commits, events, episodes and event links. `inputs/observations.sqlite` supplies frozen registry evidence. `inputs/commit_times/commit_times.csv.gz`, included in Git, supplies both recovered dates for every commit. The main analysis uses committer time; author time is evaluated separately. This command runs complete classification, verifies identities/queryability/labels and updates the same `REPORT.md`. Output directories must be new. The current integrated results are under `results_integrated/`; `results_final/` is the previous Java-only combined result and `results/` is the original unified baseline.

To replay the source-machine extraction repair and complete Stage2 as well:

```bash
python run_integrated.py --output rebuilt_from_saved_inputs --raw-dir imported/corrected/raw_commit_items --raw-dir imported/corrected/raw_commit_items_retry --raw-dir imported/nonclaude/raw_commit_items --update-report
```

This reuses `inputs/replay.sqlite` and reads the existing child-manifest cache, patch files, frozen population and six context databases at the paths recorded in `prepare_replay.py`, `run_stage12_replay.py` and `inputs/replay.manifest.json`. Those large upstream caches are external inputs. `prepare_replay.py` shows how the six-language event universe, child fetch index and patch index were joined. The imported raw corpus and those caches are needed for source-machine replay; final classification from the downloaded Stage1/2 output is portable.

`pipeline.py demo --output reproduced_demo` runs one real commit through the original extraction entry, Stage2 and saved-evidence classification. `stage3_run.py --output reproduced_baseline` reproduces the earlier unified baseline. The previous Java run and its bridges remain under `results_java_full/`.

## Full workflow and code locations

| Step | Entry points | Inputs / purpose |
|---|---|---|
| Annual commit sampling | `scripts/collection/collect-may-sampled-commits.ps1` | One daily 10-minute window, seed 202506; explicit dates and output |
| Non-Claude extension | `scripts/collection/collect-nonclaude-optimized-commits.ps1` | Four-hour windows and selected non-Claude channels |
| Retry / corrected corpus | `retry-incomplete-segments.ps1`, `scripts/processing/build-corrected-v1.ps1` | Preserve query/collection logs |
| Clean / merge | `build-cleaned-v1-and-agent-population.ps1`, `merge-agent-commit-populations.ps1` | Full agent membership and global repo–SHA union |
| Repository / commit evidence | `scripts/metadata/fetch-repo-metadata.ps1`, `fetch-commit-json-diffs-with-languages.py` | Raw commit JSON, patch index, changed files |
| Stage1 | `pipeline.py stage1` → `stage1_extract.py`; `run_stage12_replay.py` → `stage1_snapshot.py` | Local diff extraction, then verified child-snapshot structure for actual added lines |
| Context | `manifest_enrich.py`, `stage2_context.py`, `stage2_python_wheel.py` | Existing manifest/lock/context and import mappings |
| Stage2 | `pipeline.py stage2` → `stage2_build.py` | Events + optional context DB → frozen episodes |
| Registry collection | `stage3_collect.py`, `stage3_registry_collector.py` | Registry observations; OSV remains a separate axis |
| Unified classification | `src/stage3_matching.py`, `src/stage3_classify.py`, `run_stage3_replay.py` | Uniform classification of the complete rebuilt universe and proxy sensitivity |
| General new Stage2 inputs | `pipeline.py classify-staged` | Stage2 folder + local registry JSONL → new labels |
| Frozen experiment packaging | `stage3_prepare.py`, `src/stage3_prepare_*.py` | Source-machine export, not required for included-input replay |

Original stage and context entries above are under `scripts/hallucination/`, with filenames and imports renamed without an RQ prefix. The original extraction entry is retained. The integrated replay adds the child-snapshot adapter; Stage2 includes Java identity and source-aware version repairs. Use explicit input/output arguments. Context is optional and affects mapping coverage. The integrated run rebuilds all ecosystems together, preserving cross-language aggregation.

For full manifest enrichment/context rebuilding, use Python 3.11 or newer: the enrichment parser needs `tomllib` for `pyproject.toml`, `uv.lock` and other TOML files. The wrapper sets package-local import paths; when invoking source scripts directly in PowerShell, first set `$env:PYTHONPATH = (Resolve-Path ./vendor).Path`. Collection and registry refresh are separate online steps.

### Java identity pilot

The first Java pilot used Lombok, SLF4J and JUnit. Its original catalog is preserved with 30 real commits under `samples/java_identity/`. The current catalog has 1,118 exact targets supported by official documentation and cached source trees under `research/`. The prior 109-target catalog is preserved as `results_java_full/catalog.json`. `stage2_java_identity.py` combines each mapping with the nearest same-coordinate declaration.

```bash
python run_java_pilot.py --output reproduced_java
python verify_java_pilot.py --results reproduced_java
```

The pilot requires the optional historical bundle and is retained as an initial development check. The earlier complete Java result is `results_java_full/`; event-level bridges distinguish identity recovery from episode merging. `REPORT.md` presents the current integrated population. The verification command above also reads the included baseline output.

### Offline extraction and alignment of imported raw data

```bash
python pipeline.py stage1 --index imported/commit_index.csv --population imported/merged_agent_commit_population.csv --output new_stage12 --languages TypeScript JavaScript Python Rust Go Java
python pipeline.py stage2 --input-dir new_stage12 --context-db imported/context.sqlite
python pipeline.py classify-staged --input-dir new_stage12 --registry-jsonl imported/registry_evidence.jsonl --output new_labels.jsonl
```

The import index must point to existing local patch files. Omit `--context-db` only deliberately; this is not equivalent to the full contextual experiment. `classify-staged` uses committer time from the recovered supplement (or a supplied `committer_date` column). Use `--commit-times PATH` for another prepared population; missing committer time stays explicit. The frozen full run additionally integrates source-bound aliases/context and existing commit metadata through `stage3_prepare.py`.

### Collection from scratch (optional, not executed here)

Collection is a separate, explicitly invoked workflow requiring GitHub credentials and network access. The original raw corpus is already on Hugging Face. Recollection today cannot reproduce historical search responses exactly.

```powershell
& ./scripts/collection/collect-may-sampled-commits.ps1 -SinceUtc '2025-06-01T00:00:00Z' -UntilUtc '2026-06-01T00:00:00Z' -Seed 202506 -SampleMinutesPerDay 10 -OutputDir './new_raw/annual'
& ./scripts/collection/collect-nonclaude-optimized-commits.ps1 -SinceUtc '2025-06-01T00:00:00Z' -UntilUtc '2026-06-01T00:00:00Z' -Seed 202506 -SampleMinutesPerDay 240 -OutputDir './new_raw/nonclaude'
```

Set `GITHUB_TOKEN` yourself; no `.env`, credential, runtime environment, or private credential cache is included. Use the documented parameter blocks / `--help` of each subsequent collection/context entry to supply paths. Do not import collectors merely to inspect them.

## Four main labels

- `present_at_commit`: an applicable satisfying release has usable recorded time at/before the boundary.
- `hallucinated_dependency`: assumption-based operational indicator; subtype `package_no_match`, `version_no_match`, or `postdate`.
- `insufficient_evidence`: a concrete identity, parsing, request or time obstacle remains.
- `out_of_scope`: explicit upstream exclusion or source-bound local/stdlib reference under the public-registry estimand.

No-match inference assumes that the selected public registries represent the intended source and that unobserved deletion, migration or omission has not removed a satisfying historical release. Every output retains its reason and source IDs. Historical labels provide a comparison table.

`policy.json` is the human/machine-readable description of this fixed implementation, not a general policy language. Modifying it alone does not change the classifier. npm uses strict semver, normal prerelease exclusion; PyPI uses pinned packaging. Cargo supports comma-intersected comparators, caret, tilde and wildcards: bare numeric requests become caret requests, evaluated with the pinned semver engine. This fixes the old partial-zero caret bounds. Maven ranges support numeric intervals, while exact Maven matching is literal; unresolved Maven properties remain insufficient evidence. Unsupported syntax remains explicit. These are implementation limits, not proof that such dependencies are nonexistent.

Proxy-time classification and classification with proxy timestamps withheld are both reported. The 24-hour interval is a descriptive sensitivity field.

## Inspect and debug a case

Pick an ID in `results/examples.csv` or `results/episodes.csv.gz`:

```bash
python inspect_episode.py --episode EPISODE_ID --output case_trace.json
```

This exports the frozen episode, commit boundary, bound context, applicable registry observations, both new decisions and the old comparison label. Observation `origin` points to packaged raw response IDs or original fingerprinted source rows. Absolute source paths are provenance only; offline replay does not open those paths.

In PyCharm, run `pipeline.py` with `demo --output my_debug_demo` to step through Stage1 → Stage2 → saved Stage3 evidence → new classification. Useful breakpoints: `stage1_extract.run_stage1`, `stage2_episode_linker._build_commit`, `stage3_classify.scope_result`, `stage3_matching.Matcher.match`, `stage3_classify.observe` and `stage3_classify.decide`. For frozen full-run case debugging, run `inspect_episode.py` with the arguments above; use F7 to step in or F9 to reach the next breakpoint. Output paths must be new.

## Inspect the integrated extraction repair

`results_replay_v2/stage12.sqlite` stores four logical tables: commits, commit-grouped events, episodes, and commit-grouped event links. JSON bodies are zlib-compressed; `src/common.py:unpack` decodes them. Episode columns also expose ecosystem, package, query and queryability for direct SQL. `manifest_witnesses.jsonl.gz` records each applied child snapshot, its SHA-256, actual added line numbers and before/after event IDs.

`repair_replay_context.py` is the narrow completion pass used during this run to preserve already-resolved Maven properties after a reviewed context-priority correction. `repair_replay_scope.py` adds the analogous bounded completion pass for cross-manifest resolved context. A fresh replay uses both corrected linker rules directly. The before/after pass summaries preserve their effects.

The integrated verifier checks every event link and reconstructed query, snapshot event anchors, final label/time invariants and aggregate conservation. `identity_transitions.csv` compares stable episode IDs against the unified baseline; added and removed/replaced identities are separate categories.

To export the four structural tables to a new folder without rerunning the experiment:

```bash
python export_main_tables.py --output tables_stage12
python verify_exported_tables.py --tables tables_stage12
```

The exporter validates the source database hash, preserves all contracted fields, and reads back all CSV records to check headers, widths, row counts and gzip integrity. The second command checks exact episode-ID equality against the current final-label table. Its verification receipt is also included under `delivery/`.

## Query kinds for a possible freshness analysis

The current population contains 419,808 exact, 483,471 range and 431,854 name-only
queries. `results_query_kinds/query_kind_counts.csv` includes the ecosystem breakdown.
These are query counts; freshness eligibility additionally depends on selected-version
and release-history evidence. With the reproduction data downloaded, reproduce this
read-only aggregation in a new output directory:

```bash
python summarize_query_kinds.py --output reproduced_query_kinds
```

## Commit-time update, 7 October 2026

The current result version is `canonical_v3`. Both author and committer dates are available for all commits. Main labels use committer time throughout; `author_*` output columns and `author_sensitivity.csv` provide a separate author-time comparison. `inputs/commit_times/manifest.json` binds the date table to the unchanged Stage1/2 and registry inputs.

The four structural CSV tables are unchanged. Replace the final-label file with the version identified in `delivery/data_bundle_files.csv`; use the matching code, report and small tables from Git. The historical baseline commands retain their earlier time policy for reproducing those named outputs.

## Stratified tables and release-evidence coverage

`results_paper_tables/` contains agent, ecosystem and agent-by-ecosystem counts, queryable repository counts, multi-agent overlaps and release-evidence coverage. Historical canonical tables and the revised four-category results are named separately. See its `README.md` for definitions.

To regenerate these small tables from the downloaded results and registry evidence:

```bash
python summarize_paper_tables.py --output reproduced_paper_tables
```

The complete commit-agent relation and stable repository identities are included in Git as `inputs/analysis_membership/commits.csv.gz`. No additional large data download is needed.


## Postdate intervals

`results_integrated/postdate_intervals.csv` reports global and ecosystem release-to-committer
intervals, with shares within the postdate population. `postdate_intervals.json` binds
the summary to the final episode file. Regenerate from downloaded results with:

```bash
python summarize_postdate_intervals.py --output reproduced_postdate_intervals
```

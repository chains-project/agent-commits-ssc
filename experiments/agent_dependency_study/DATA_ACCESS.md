# Current experiment data and reproduction

## Version and availability

This code accompanies the **7 October 2026 committer-time update (`canonical_v3`)**: 343,790 commits,
1,679,785 dependency episodes and 1,335,133 queryable episodes. Current results
are under `results_integrated/`; the methods and counts are in [REPORT.md](https://github.com/chains-project/agent-commits-ssc/tree/feat/dependency-study/experiments/agent_dependency_study/REPORT.md).

**Data download:** [Google Drive: agent-commits-ssc](https://drive.google.com/drive/folders/1Qp-xgDnHjzgSQWFrJgoIo_5FvHnEq7Ta?usp=sharing).
**Code, report and small tables:** [GitHub experiment directory](https://github.com/chains-project/agent-commits-ssc/tree/feat/dependency-study/experiments/agent_dependency_study).
Use the `feat/dependency-study` branch. The shared Drive folder
contains `results` and `reproductions`; the layout below explains how they fit the
Git checkout. File hashes in Git identify the prepared version.

The [raw commit corpus](https://huggingface.co/datasets/ASSERT-KTH/agent-commits-raw)
is a separate upstream dataset. It is not the download location for these newly
rebuilt experiment tables.

## Data-only downloads

Clone this repository for code, reports, small tables and hash/schema manifests.
The external download contains only large data files, split into two bundles:

| Bundle | Size | Contents |
| --- | ---: | --- |
| `results/agent_dependency_study/` | 728,043,634 bytes (728 MB) | Four current structural CSV.gz tables and the current final-label CSV.gz |
| `reproductions/agent_dependency_study/` | 3,179,028,624 bytes (3.18 GB) | Stage1/2 database, registry evidence, snapshot witnesses and baseline needed for verification |

For result analysis, download only `results`. To rerun classification and the full
verifier, download both. Copy each bundle's `agent_dependency_study/` contents into
this repository's `experiments/agent_dependency_study/`, preserving relative paths.
Code, documentation, small manifests and earlier development outputs are supplied
through Git or retained as historical artifacts rather than repeated in these downloads.

The reproduction bundle contains:

| Path | Purpose |
| --- | --- |
| `inputs/observations.sqlite` | Frozen registry observations and applicable contextual facts |
| `results_replay_v2/stage12.sqlite` | Verified rebuilt commits, events, episodes and links |
| `results_replay_v2/manifest_witnesses.jsonl.gz` | Snapshot and added-line witnesses |
| `results/episodes.csv.gz` | Historical baseline used only for identity-transition verification |

The complete nine-file inventory, sizes and SHA-256 hashes are in
`delivery/data_bundle_files.csv`. Earlier Java inputs/results and raw development
diagnostics remain historical artifacts; they are unnecessary for current-result analysis.

## Four structural tables and final labels

| File under `tables_stage12/` | Rows | Key |
| --- | ---: | --- |
| `commits.csv.gz` | 343,790 | `commit_id` |
| `events.csv.gz` | 5,254,894 | `event_id` |
| `episodes.csv.gz` | 1,679,785 | `episode_id` |
| `episode_event_links.csv.gz` | 5,290,230 | `episode_id`, `event_id` |

These are exports of the current Stage1/2 database, not older canonical tables.
The current final-label table is `results_integrated/episodes.csv.gz`; join it to
the structural episode table by `episode_id`. Exact ID-set equality has been
verified. The stored `commits.agent` field is an upstream value, not the complete
multi-agent membership relation.


Each final-label row includes `label`, `subtype` and `reason`. Examples include
`matching_release_before_boundary`, `package_no_match`, `version_no_match`,
`postdate`, `registry_request_failed` and `matching_release_time_missing`.
`upstream_reasons` retains extraction/alignment notes; `evidence_ids`,
`matching_version`, `matching_time` and `delta_seconds` provide supporting details.
Use `episode_id` to connect labels to structural episodes and event links.
`results_integrated/reasons.csv` in Git gives counts by ecosystem, label and reason.

Column lists, row counts and SHA-256 values are recorded in
`delivery/main_tables_manifest.json`. The exact episode-ID comparison is in
`delivery/main_tables_label_join_verification.json`. File-level data inventories
are under `delivery/`. The `results_replay_v2` database hash is:

```text
8eb8a3ed1fc4b11704985ffdfb6c6ecba04c3411391e0a24d4fa0e5416a1b7e1
```

## Download and place the files

1. Download the code from the GitHub branch linked above, or clone it:

   ```bash
   git clone --branch feat/dependency-study https://github.com/chains-project/agent-commits-ssc.git
   cd agent-commits-ssc/experiments/agent_dependency_study
   ```

2. Download `results` from Drive. Copy the contents of its `agent_dependency_study`
   folder into this directory. You should then have
   `tables_stage12/commits.csv.gz` and `results_integrated/episodes.csv.gz` here.
3. For full reproduction, also download `reproductions` and copy its
   `agent_dependency_study` contents into the same directory. You should then
   have `inputs/observations.sqlite` and `results_replay_v2/stage12.sqlite` here.
   Some download tools add an outer folder or ZIP wrapper; preserve the paths
   relative to `agent_dependency_study`, rather than nesting that folder again.

Read `.csv.gz` files directly with a gzip-capable CSV reader, for example
`pandas.read_csv(path, compression="gzip")`. Join the structural episodes to final
labels on `episode_id`, and join events through `episode_event_links`; joining
only on package name would merge unrelated commits and version requests.

## Run the code

Run commands from `experiments/agent_dependency_study/`. Python and Node.js must
be available; this run used Python 3.9.12 and Node.js 23.7.0. Small pinned parser
dependencies and their licenses are included. The commands below are offline.

Code-only tests:

```bash
python -m unittest discover -s tests -v
```

After cloning the repository and adding the results bundle, verify the structural episode and final-label IDs:

```bash
python verify_exported_tables.py --tables tables_stage12
```

After adding both bundles, run the full verifier:

```bash
python verify_integrated_results.py
```

Reclassify the complete rebuilt population and run the verifier:

```bash
python run_integrated.py --reuse-stage12 results_replay_v2 --output reproduced
```

`reproduced` must be a new directory. Use `--node /path/to/node` when required.
To export the four structural tables again:

```bash
python export_main_tables.py --output exported_tables
python verify_exported_tables.py --tables exported_tables
```

Replaying the earlier extraction/context stages additionally requires the
external patch corpus, child-manifest cache, frozen population and context
databases described in [README.md](https://github.com/chains-project/agent-commits-ssc/tree/feat/dependency-study/experiments/agent_dependency_study/README.md). Together, the results and reproduction bundles support the
current final classification and its full verification.

## Committer-time update

The four structural CSVs and the reproduction bundle retain their existing hashes. The updated final-label CSV uses committer time for every temporal decision. Its `author_*` columns record the separate author-time sensitivity analysis. Both dates are supplied in Git as `inputs/commit_times/commit_times.csv.gz` (join on `commit_id`). The supplement is about 33 MB and is required by the current classification entry point.

When updating an earlier download, replace only `results/agent_dependency_study/results_integrated/episodes.csv.gz` and update the Git checkout. The current hash is listed in `delivery/data_bundle_files.csv`. The Drive-root README may also be replaced with this guide. Historic OSV counts remain explicitly tied to the old canonical population and author boundary.

## Stratified tables and release-evidence coverage

`results_paper_tables/` contains agent, ecosystem and agent-by-ecosystem counts, queryable repository counts, multi-agent overlaps and release-evidence coverage. Historical canonical tables and the revised four-category results are named separately. See its `README.md` for definitions.

To regenerate these small tables from the downloaded results and registry evidence:

```bash
python summarize_paper_tables.py --output reproduced_paper_tables
```

The complete commit-agent relation and stable repository identities are included in Git as `inputs/analysis_membership/commits.csv.gz`. No additional large data download is needed.

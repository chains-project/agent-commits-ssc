# Agent Commits SSC

This repository contains the code, tests, compact results, and reproducibility
metadata for an empirical study of AI coding-agent commits on GitHub. The
initial release covers RQ1 corpus construction and RQ2 detection and
conservative adjudication of hallucinated dependencies. RQ3 and RQ4 feasibility
work is not included as a formal result.

## Repository contents

- scripts/collection, scripts/processing, and scripts/metadata contain the RQ1
  collection and population-building pipeline.
- scripts/hallucination contains the current RQ2 Stage 1-3 pipeline.
- scripts/rq2/adjudication contains the consolidated final adjudicator and the
  independent commented-manifest guard.
- tests contains unit, regression, and offline synthetic scenario tests.
- data/rq2 contains the public row-level adjudication table and indexes for the
  separately distributed canonical tables.
- results contains compact RQ1/RQ2 summaries and machine-readable validation
  records.
- scripts/exploration/model_observability, data/model_observability, and
  results/model_observability contain the exploratory harness-signature and
  underlying-model observability supplement.
- docs/artifact-catalog.md describes every public file.
- docs/claude-linked-author-exploration.md reports the exploratory RQ1
  linked-author Top-10, public profile context, sensitivity checks, and
  interpretation limits.

Historical repair wrappers, internal plans, reports, display figures, caches,
raw review packets, and large intermediate tables are excluded.

## Install

Using conda:

    conda env create -f environment.yml
    conda activate agent-commits-ssc

Using an existing Python 3.11 environment:

    python -m pip install -r requirements.txt

GitHub collection commands require GITHUB_TOKEN in the process environment.
Large-data workflows use THESIS_DATA_ROOT or explicit command-line paths. No
token or external data is required for the offline test suites below.

## Validate the public package

    python -m unittest tests.test_rq2_adjudication
    python -m unittest tests.test_rq2_adjudication_synthetic_chain
    python -m unittest tests.test_rq2_stage4_synthetic_chain
    python -m unittest tests.test_rq2_public_analysis
    python scripts/rq2/analysis/verify_published_product.py --root .

The validation surface consists of code tests, 49 Stage 4 synthetic scenarios,
nine final-adjudication scenarios, nine recorded legacy/current equivalence
comparisons, and 80 final-analysis consistency checks. These checks validate
implementation behavior and data-product consistency; they do not establish
external ground truth.

## Exploratory model-attribution feasibility

The [exploratory report](docs/harness-model-observability.md) asks whether public
commit artifacts can identify the model that actually executed a coding task.
It distinguishes harness signatures, declared models and execution evidence.
The 120-case check found model declarations but did not establish actual
execution; existing diff/config inspection added no accepted model-binding
label beyond message/trailer evidence. This finding applies to the inspected
material, not to every possible source of attribution evidence.

Run its offline tests and descriptive sample-count replay with:

    python -m unittest discover -s tests -p "test_*.py"
    python scripts/exploration/model_observability/summarize_attribution_feasibility.py --root .

The [codebook](docs/model-observability-codebook.md) defines the frozen labels.
The [five-slide presentation](docs/presentations/harness-model-observability.pptx)
illustrates the signature and keyword-discovery results; read it alongside the
report for the model-attribution conclusion. Compact tables support count
replay. Full scans and evidence reinspection require separately held inputs.

## RQ2 result boundary

The public adjudication CSV contains 74,618 candidate rows: 322 confirmed
hallucinations, 382 probable hallucinations, 21,821 not hallucination, and
52,093 indeterminate.

Absence from a current registry snapshot is not by itself evidence of
hallucination. The method considers temporal registry evidence, manifest and
lockfile context, dependency-key matching, direct references, patch evidence,
and fixed sidecar evidence where available.

## Data availability

The repository includes compact manifests, summaries, validation JSON, and the
row-level final adjudication CSV. The 24 canonical RQ2 tables and large RQ1/raw
corpora remain external because of size and redistribution review. Their file
sizes, SHA-256 values, schemas, and generation boundaries are recorded in
data/rq2/canonical_table_index.json and docs/data-availability.md. Download URLs
remain placeholders until a separate upload is authorized.

Code is released under the MIT License. Dataset redistribution terms and
third-party rights are documented separately from the software license.

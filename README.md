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
- docs/artifact-catalog.md describes every public file.

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
implementation behavior and data-product consistency; they are not an
independent human gold standard.

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

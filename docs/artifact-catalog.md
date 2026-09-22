# Artifact catalog

This catalog gives the role and provenance of every file in the initial public
export. Generated public documentation and indexes are identified explicitly.
Historical repair wrappers, reports, internal plans, caches, and external full
corpora are not part of this tree.

## Directory responsibilities

| Directory | Responsibility |
|---|---|
| scripts/collection | RQ1 GitHub search, segmentation, retry, and channel sampling |
| scripts/processing | RQ1 cleaning, deduplication, population merging, and summaries |
| scripts/metadata | RQ1 repository, commit, diff, and language metadata |
| scripts/hallucination | RQ2 Stage 1-3 extraction, linking, registry, and evidence pipeline |
| scripts/rq2/adjudication | Current final adjudicator and independent comment guard |
| scripts/rq2/analysis | Path-neutral public summary and verification tools |
| scripts/exploration/model_observability | Exploratory harness-signature and public model-observability analysis |
| tests | Unit, regression, and offline synthetic scenario tests |
| data/rq2 | Public row-level result and external canonical-data indexes |
| data/model_observability | Frozen 120-case sample design, declarations and evidence-availability fields |
| results/summary | Compact scientific summaries and provenance manifests |
| results/validation | Machine-readable validation evidence |
| results/model_observability | Signature, discovery, sample-label and evidence-coverage counts |
| docs | Method, schema, data availability, and artifact documentation |

## Model-observability supplement

The compact tables and summary support the local count-replay command in the
report. Original scanners and sampling/context runners additionally require the
separately held frozen corpus and input manifest.

The consensus runner requires five external files in its `--run-dir`:
`EXPLORATORY_AGENT_A_CODES.csv`, `EXPLORATORY_AGENT_B_CODES.csv`,
`EXPLORATORY_AGENT_RECODE_PACKAGE.csv`, `EXPLORATORY_AGENT_A_RECODE.csv`
and `EXPLORATORY_AGENT_B_RECODE.csv`. It combines existing judgments; the two
compact public tables alone do not regenerate them.

| Path | Role |
|---|---|
| docs/harness-model-observability.md | English report on the bounded feasibility of actual underlying-model attribution |
| docs/presentations/harness-model-observability.pptx | Five-slide signature/discovery presentation accompanying the report |
| docs/model-observability-codebook.md | Frozen X0-X3/XC exploratory coding rules and reporting restrictions |
| data/model_observability/EXPLORATORY_AGENT_CONSENSUS.csv | Frozen labels, declared-model tokens and evidence-source categories for 120 cases |
| data/model_observability/EXPLORATORY_PROBABILITY_SAMPLE.csv | Existing sample-design fields plus frozen diff availability/status; no message or patch text |
| results/model_observability/EXPLORATORY_ATTRIBUTION_FEASIBILITY_SUMMARY.json | Exact descriptive label, source, stratum and diff-coverage counts |
| results/model_observability/DOMESTIC_MODEL_DISCOVERY_ANNOTATION_SUMMARY.csv | Selected-context discovery label counts by model family |
| results/model_observability/DOMESTIC_MODEL_KEYWORD_COUNTS_COMPLETE.csv | Complete frozen lexical signal counts |
| results/model_observability/EXPLORATORY_LOCAL_SIGNAL_COUNTS.csv | E1 message-census stratum and signal counts |
| results/model_observability/QWEN_H1_INCREMENTAL_SUMMARY.json | Counts for the producer-documented exact Qwen H1 signature |
| scripts/exploration/model_observability/apply_domestic_discovery_annotations.py | Applies annotations only after verifying the exact frozen sample hash |
| scripts/exploration/model_observability/build_domestic_model_context_sample.py | Builds the deterministic balanced discovery sample |
| scripts/exploration/model_observability/build_exploratory_probability_sample.py | Builds the two-stage stratified probability sample |
| scripts/exploration/model_observability/domestic_discovery_common.py | Shared lexical scanning, hashing, and table helpers |
| scripts/exploration/model_observability/DOMESTIC_MODEL_DISCOVERY_LEXICON.v0.1.json | Frozen domestic-model discovery aliases and exclusions |
| scripts/exploration/model_observability/EXPLORATORY_MODEL_PROXY_LEXICON.v1.json | Frozen public-evidence proxy lexicon |
| scripts/exploration/model_observability/exploratory_observability_common.py | Shared bounded-context, pseudonymization, and I/O helpers |
| scripts/exploration/model_observability/extract_exploratory_diff_context.py | Extracts bounded same-commit context; rejects missing indexed-ok artifacts |
| scripts/exploration/model_observability/finalize_exploratory_agent_coding.py | Combines two agent-coded outputs under the frozen XC rule; requires external coding/recode inputs and rejects duplicate ranks |
| scripts/exploration/model_observability/offline_signal_scanner.py | Offline H/M/HM registry scanner |
| scripts/exploration/model_observability/summarize_attribution_feasibility.py | Recounts compact sample evidence and checks the published descriptive summary |
| scripts/exploration/model_observability/run_exploratory_local_census.py | Builds the matched-frame public-signal census |
| scripts/exploration/model_observability/scan_domestic_model_keywords.py | Scans frozen inputs with the domestic-model lexicon after an explicit external baseline-closure gate |
| scripts/exploration/model_observability/scan_qwen_verified_signature.py | Scans explicit inputs for the exact Qwen H1 trailer |
| scripts/exploration/model_observability/SIGNATURE_REGISTRY.json | Sealed baseline signature registry |
| scripts/exploration/model_observability/SIGNATURE_REGISTRY.v1.1.0.json | Registry revision containing the verified Qwen H1 rule |
| tests/test_domestic_model_discovery.py | Offline discovery lexicon and sampling tests |
| tests/test_attribution_feasibility.py | Count replay, missing-case, disagreement and evidence-availability integrity tests |
| tests/test_exploratory_observability.py | Public-evidence census and bounded-context tests |
| tests/test_network_verified_registry.py | Qwen registry rule and exclusion tests |
| tests/test_offline_signal_scanner.py | Offline signature scanner tests |
| tests/fixtures/model_observability/signature_cases.json | Small positive, negative, and excluded signature fixtures |

## Files

| Path | RQ | Role | Source or provenance | Regenerable |
|---|---|---|---|---|
| .github/workflows/ci.yml | shared | CI for pinned-environment unit tests, synthetic Stage 4, link checks, and secret/large-file guards | PROPOSED_NEW/.github/workflows/ci.yml | no |
| .gitignore | shared | repository ignore rules | .gitignore | no |
| LICENSE | shared | MIT license for original repository contents | PROPOSED_NEW/LICENSE | no |
| README.md | shared | public overview and quick start | PROPOSED_NEW/README.md | no |
| THIRD_PARTY_NOTICES.md | shared | third-party dependency and reused-code attribution | PROPOSED_NEW/THIRD_PARTY_NOTICES.md | yes |
| data/rq2/canonical_table_index.json | RQ2 | canonical entry, compact final result, manifest, or final verification | external source; see data-availability.md | yes |
| data/rq2/experiment_complete.json | RQ2 | canonical entry, compact final result, manifest, or final verification | external source; see data-availability.md | yes |
| data/rq2/public_product_manifest.json | RQ2 | canonical entry, compact final result, manifest, or final verification | external source; see data-availability.md | yes |
| data/rq2/v2_native_adjudication.csv | RQ2 | 74,618-row final candidate adjudication | external source; see data-availability.md | yes |
| docs/artifact-catalog.md | mixed | per-file public artifact catalog | generated during clean export | yes |
| docs/claude-linked-author-exploration.md | RQ1 | exploratory linked-author Top-10, public-profile context, sensitivity checks, and interpretation limits | frozen RQ1 population, reviewed internal analysis, and public self-presentations | no |
| docs/data-availability.md | shared | Git/external-data boundary and download index | PROPOSED_NEW/docs/data-availability.md | no |
| docs/data-dictionary.md | RQ1/RQ2 | schemas, fields, primary keys, and redaction notes | PROPOSED_NEW/docs/data-dictionary.md | no |
| docs/rq1-method.md | RQ1 | RQ1 methods and limitations | PROPOSED_NEW/docs/rq1-method.md | no |
| docs/rq2-method.md | RQ2 | RQ2 Stage 1-4 methods and label semantics | PROPOSED_NEW/docs/rq2-method.md | no |
| docs/scenario-testing.md | RQ2 | six-language scenario and Oracle documentation | PROPOSED_NEW/docs/scenario-testing.md | no |
| environment.yml | shared | pinned runtime environment | PROPOSED_NEW/environment.yml | no |
| requirements.txt | shared | pinned Python dependencies | PROPOSED_NEW/requirements.txt | no |
| results/summary/rq1/data/agent_commit_population_summary.csv | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/agent_counts_clean.csv | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/cap_bound_terminal_segments.csv | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/channel_counts_clean.csv | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/corrected_population_manifest.json | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/filter_counts_clean.csv | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/merged_agent_summary.csv | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/merged_source_summary.csv | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/merged_summary.csv | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/nonclaude_cleaned_manifest.json | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/query_status_counts_clean.csv | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/repo_language_join_manifest.json | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/repo_metadata_manifest.json | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq1/data/sampled_windows_clean.csv | RQ1 | small RQ1 summary or provenance manifest | external source; see data-availability.md | yes |
| results/summary/rq2/canonical-product-readme.md | RQ2 | canonical entry, compact final result, manifest, or final verification | external source; see data-availability.md | yes |
| results/summary/rq2/v2_final_by_language.csv | RQ2 | canonical entry, compact final result, manifest, or final verification | external source; see data-availability.md | yes |
| results/summary/rq2/v2_final_summary.json | RQ2 | canonical entry, compact final result, manifest, or final verification | external source; see data-availability.md | yes |
| results/validation/rq2/adjudication_equivalence_summary.json | RQ2 | legacy-v6 versus versionless field-equivalence summary | results/rq2_adjudication_consolidation_20260819/equivalence_summary.json | yes |
| results/validation/rq2/adjudication_synthetic_summary.json | RQ2 | current adjudication synthetic validation summary | results/rq2_adjudication_synthetic_chain_20260819_consolidated/summary.json | yes |
| results/validation/rq2/final_analysis_verification.json | RQ2 | canonical entry, compact final result, manifest, or final verification | external source; see data-availability.md | yes |
| results/validation/rq2/stage4_summary.json | RQ2 | 49-pass public Stage 4 scenario summary | PROPOSED_REGENERATED/results/validation/rq2/stage4_summary.json | yes |
| scripts/collection/collect-channel-overlap.ps1 | RQ1 | GitHub Search collection and channel audit | scripts/collection/collect-channel-overlap.ps1 | no |
| scripts/collection/collect-commits.py | RQ1 | GitHub Search collection and channel audit | scripts/collection/collect-commits.py | no |
| scripts/collection/collect-may-sampled-commits.ps1 | RQ1 | GitHub Search collection and channel audit | scripts/collection/collect-may-sampled-commits.ps1 | no |
| scripts/collection/collect-nonclaude-optimized-commits.ps1 | RQ1 | GitHub Search collection and channel audit | scripts/collection/collect-nonclaude-optimized-commits.ps1 | no |
| scripts/collection/compare-windows.py | RQ1 | GitHub Search collection and channel audit | scripts/collection/compare-windows.py | no |
| scripts/collection/retry-incomplete-segments.ps1 | RQ1 | GitHub Search collection and channel audit | scripts/collection/retry-incomplete-segments.ps1 | no |
| scripts/collection/test-codex-new-channels.ps1 | RQ1 | GitHub Search collection and channel audit | scripts/collection/test-codex-new-channels.ps1 | no |
| scripts/figures/analyze-agent-repo-figures.py | RQ1 | RQ1 descriptive figure generation | scripts/figures/analyze-agent-repo-figures.py | no |
| scripts/hallucination/RQ2_STAGE3_EVIDENCE_INPUT.md | RQ2 | RQ2 v2 offline evidence input contract | scripts/hallucination/RQ2_STAGE3_EVIDENCE_INPUT.md | no |
| scripts/hallucination/repair_rq2_stage3_metadata.py | RQ2 | current v2 Stage 3 narrow metadata recovery tool | scripts/hallucination/repair_rq2_stage3_metadata.py | no |
| scripts/hallucination/rq2_pypi_requirement.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_pypi_requirement.py | no |
| scripts/hallucination/rq2_stage1_extract.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage1_extract.py | no |
| scripts/hallucination/rq2_stage1_manifest_diff.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage1_manifest_diff.py | no |
| scripts/hallucination/rq2_stage1_streaming.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage1_streaming.py | no |
| scripts/hallucination/rq2_stage2_alignment.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage2_alignment.py | no |
| scripts/hallucination/rq2_stage2_build.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage2_build.py | no |
| scripts/hallucination/rq2_stage2_context.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage2_context.py | no |
| scripts/hallucination/rq2_stage2_episode_linker.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage2_episode_linker.py | no |
| scripts/hallucination/rq2_stage2_python_wheel.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage2_python_wheel.py | no |
| scripts/hallucination/rq2_stage2_streaming.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage2_streaming.py | no |
| scripts/hallucination/rq2_stage3_advisory_evidence.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage3_advisory_evidence.py | no |
| scripts/hallucination/rq2_stage3_apply.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage3_apply.py | no |
| scripts/hallucination/rq2_stage3_collect.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage3_collect.py | no |
| scripts/hallucination/rq2_stage3_legacy_evidence.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage3_legacy_evidence.py | no |
| scripts/hallucination/rq2_stage3_osv_collector.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage3_osv_collector.py | no |
| scripts/hallucination/rq2_stage3_registry_collector.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage3_registry_collector.py | no |
| scripts/hallucination/rq2_stage3_streaming.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage3_streaming.py | no |
| scripts/hallucination/rq2_stage3_version_query.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage3_version_query.py | no |
| scripts/hallucination/rq2_stage_schema.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_stage_schema.py | no |
| scripts/hallucination/rq2_streaming_store.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/rq2_streaming_store.py | no |
| scripts/hallucination/rq3_cache_storage.py | RQ2 | legacy-named compatibility module required by RQ2 v2 collector | scripts/hallucination/rq3_cache_storage.py | no |
| scripts/hallucination/rq3_registry_label.py | RQ2 | legacy-named compatibility module required by RQ2 v2 collector | scripts/hallucination/rq3_registry_label.py | no |
| scripts/hallucination/run_rq2_phase9_experiment.py | RQ2 | RQ2 v2 executable pipeline or evidence contract | scripts/hallucination/run_rq2_phase9_experiment.py | no |
| scripts/metadata/fetch-commit-json-diffs-with-languages.py | RQ1 | repository metadata and diff/commit corpus collection | scripts/metadata/fetch-commit-json-diffs-with-languages.py | no |
| scripts/metadata/fetch-language-commit-diffs.ps1 | RQ1 | repository metadata and diff/commit corpus collection | scripts/metadata/fetch-language-commit-diffs.ps1 | no |
| scripts/metadata/fetch-repo-metadata.ps1 | RQ1 | repository metadata and diff/commit corpus collection | scripts/metadata/fetch-repo-metadata.ps1 | no |
| scripts/metadata/repair_commit_json_changed_files.py | RQ1 | repository metadata and diff/commit corpus collection | scripts/metadata/repair_commit_json_changed_files.py | no |
| scripts/processing/add-repo-language-to-commits.py | RQ1 | population cleaning, correction, merge, and enrichment | scripts/processing/add-repo-language-to-commits.py | no |
| scripts/processing/build-cleaned-v1-and-agent-population.ps1 | RQ1 | population cleaning, correction, merge, and enrichment | scripts/processing/build-cleaned-v1-and-agent-population.ps1 | no |
| scripts/processing/build-corrected-v1.ps1 | RQ1 | population cleaning, correction, merge, and enrichment | scripts/processing/build-corrected-v1.ps1 | no |
| scripts/processing/merge-agent-commit-populations.ps1 | RQ1 | population cleaning, correction, merge, and enrichment | scripts/processing/merge-agent-commit-populations.ps1 | no |
| scripts/processing/write-summaries-fast.ps1 | RQ1 | population cleaning, correction, merge, and enrichment | scripts/processing/write-summaries-fast.ps1 | no |
| scripts/rq2/adjudication/apply_commented_manifest_corrections.py | RQ2 | versionless commented-manifest correction guard | scripts/rq2/adjudication/apply_commented_manifest_corrections.py | no |
| scripts/rq2/adjudication/build_final_adjudication.py | RQ2 | versionless current final adjudication module | scripts/rq2/adjudication/build_final_adjudication.py | no |
| scripts/rq2/analysis/summarize_final_adjudication.py | RQ2 | path-neutral aggregation of final adjudication into published summaries | PROPOSED_NEW/scripts/rq2/analysis/summarize_final_adjudication.py | no |
| scripts/rq2/analysis/verify_published_product.py | RQ2 | read-only verification of public hashes, schemas, counts, rates, and confidence intervals | PROPOSED_NEW/scripts/rq2/analysis/verify_published_product.py | no |
| tests/fixtures/rq2_adjudication_synthetic_v1/scenarios.json | RQ2 | offline adjudication scenario fixture | tests/fixtures/rq2_adjudication_synthetic_v1/scenarios.json | no |
| tests/fixtures/rq2_synthetic_pipeline_v2/migration.json | RQ2 | unit/scenario test or synthetic fixture | tests/fixtures/rq2_synthetic_pipeline_v2/migration.json | no |
| tests/fixtures/rq2_synthetic_pipeline_v2/output_schema_v2.json | RQ2 | unit/scenario test or synthetic fixture | tests/fixtures/rq2_synthetic_pipeline_v2/output_schema_v2.json | no |
| tests/fixtures/rq2_synthetic_pipeline_v2/scenarios.json | RQ2 | unit/scenario test or synthetic fixture | tests/fixtures/rq2_synthetic_pipeline_v2/scenarios.json | no |
| tests/rq2_adjudication_fixture_materializer.py | RQ2 | fixed adjudication sidecar materializer | tests/rq2_adjudication_fixture_materializer.py | no |
| tests/rq2_stage4_fixture_materializer.py | RQ2 | unit/scenario test or synthetic fixture | tests/rq2_stage4_fixture_materializer.py | no |
| tests/run_rq2_adjudication_synthetic_chain.py | RQ2 | end-to-end current adjudication scenario runner | tests/run_rq2_adjudication_synthetic_chain.py | no |
| tests/run_rq2_stage4_synthetic_chain.py | RQ2 | unit/scenario test or synthetic fixture | tests/run_rq2_stage4_synthetic_chain.py | no |
| tests/test_commit_changed_files_repair.py | RQ1 | unit/scenario test or synthetic fixture | tests/test_commit_changed_files_repair.py | no |
| tests/test_rq2_adjudication.py | RQ2 | current adjudication semantic and guard tests | tests/test_rq2_adjudication.py | no |
| tests/test_rq2_adjudication_synthetic_chain.py | RQ2 | adjudication scenario regression test | tests/test_rq2_adjudication_synthetic_chain.py | no |
| tests/test_rq2_output_schema_v2.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_output_schema_v2.py | no |
| tests/test_rq2_public_analysis.py | RQ2 | unit tests for public summary and verification adapters | PROPOSED_NEW/tests/test_rq2_public_analysis.py | no |
| tests/test_rq2_pypi_markers.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_pypi_markers.py | no |
| tests/test_rq2_stage1_extract.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage1_extract.py | no |
| tests/test_rq2_stage1_manifest_diff.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage1_manifest_diff.py | no |
| tests/test_rq2_stage2_alignment.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage2_alignment.py | no |
| tests/test_rq2_stage2_build.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage2_build.py | no |
| tests/test_rq2_stage2_context.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage2_context.py | no |
| tests/test_rq2_stage2_episode_linker.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage2_episode_linker.py | no |
| tests/test_rq2_stage2_streaming.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage2_streaming.py | no |
| tests/test_rq2_stage3_advisory_evidence.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage3_advisory_evidence.py | no |
| tests/test_rq2_stage3_apply.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage3_apply.py | no |
| tests/test_rq2_stage3_collector.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage3_collector.py | no |
| tests/test_rq2_stage3_streaming.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage3_streaming.py | no |
| tests/test_rq2_stage3_version_query.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage3_version_query.py | no |
| tests/test_rq2_stage4_synthetic_chain.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage4_synthetic_chain.py | no |
| tests/test_rq2_stage_schema.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_stage_schema.py | no |
| tests/test_rq2_streaming_store.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_streaming_store.py | no |
| tests/test_rq2_synthetic_oracle_v2.py | RQ2 | unit/scenario test or synthetic fixture | tests/test_rq2_synthetic_oracle_v2.py | no |

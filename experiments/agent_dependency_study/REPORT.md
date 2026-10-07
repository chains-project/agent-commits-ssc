# Agent dependency study: integrated results

Result version: `canonical_v3`.

## Population and workflow

The current run contains **1,679,785 dependency episodes**, including **1,335,133 queryable episodes**, from **343,790 commits**. It rebuilds Stage2 across all six source languages and classifies every resulting episode using the saved registry evidence. Raw collection: [ASSERT-KTH/agent-commits-raw](https://huggingface.co/datasets/ASSERT-KTH/agent-commits-raw).

The workflow is collection and global commit deduplication → Stage1 added imports and manifest declarations → Stage2 identity/version alignment and episode aggregation → registry observations → uniform final classification. An episode key is commit, ecosystem, normalized dependency identity, query kind and query value. JS and TS references merge when that entire key agrees. Different version requests remain separate.

Queryable means an exact, range or name-only Stage2 request. The denominator uses the rebuilt episodes and the four categories below. Earlier canonical Confirmed/Probable counts use different rules and remain a separate historical result.

## Why extraction and alignment were rerun

The original manifest parser could miss additions when the diff hunk omitted a dependency-section heading. Full child manifests had already been fetched for some commits, but their structure was not used to restore all added declarations. Requirements continuations could also produce incomplete constraints. These affect event extraction and therefore both episode identities and denominators.

The repair uses saved child manifests to identify the structure around added lines. It also joins multiline requirements and handles comments correctly. Only added declarations become new dependency events; unchanged declarations supply context.

Saved snapshots and patches allowed inspection of 181,739 commits. Another 167,353 existing commits retain their previous extraction, including 40,880 with manifest events. Missing or mismatched snapshots also retain the previous extraction. The table below gives file-level coverage within the inspected subset. Existing import events and registry evidence are reused.

The revised extraction produced **5,254,894 events**. It added 27,003 event records, removed 105, and changed events in 11,827 commits. It also recovered dependency events from 34 additional commits.

| Manifest replay measure | Count |
| --- | --- |
| changed_manifest_files | 121,232 |
| snapshot_applied | 63,882 |
| manifest_snapshot_not_indexed | 57,133 |
| snapshot_cache_missing | 0 |
| snapshot_line_mismatch | 0 |
| snapshot_text_mismatch | 181 |
| snapshot_parse_failed | 36 |
| non_research_path | 0 |

## Ecosystem and identity repairs

Java now uses a catalog of **1,118 exact import targets across 28 Maven artifacts**, supported by official documentation and source code. This connects recognized imports to queryable artifact coordinates. A nearby declaration for that artifact supplies the version when available; otherwise the query uses its name. Local and first-party references remain excluded.

| Java/Maven stage | All episodes | Queryable | Insufficient evidence |
| --- | --- | --- | --- |
| Before identity repair | 177,735 | 22,563 | 152,640 |
| Previous Java-only repair | 155,521 | 46,148 | 106,854 |
| Current integrated run | 142,275 | 48,985 | 90,602 |

These are episode counts after each stage's own aggregation. The reduction reflects both recovered identities and merged references.

Version handling was also corrected. Python wildcard and supported Poetry constraints are interpreted as ranges. Maven literal versions use exact matching, and resolved property values are preserved. Lockfile context applies only to the corresponding manifest and package, preventing one subproject's version from overriding another's declaration. Go keeps its module-context matching and local-module exclusions. Stage2 then combines the events across all six languages.

Property-context corrections affected 513 commits, and manifest-context matching corrections affected 1,555; these groups may overlap. Examples are saved in `results_remaining_review/`.

Existing local and standard-library exclusions are retained where their supporting events still apply. Changed references are reassessed. Matching follows each ecosystem's version syntax; supported forms and library versions are listed in `README.md`.

## Final classification

1. `present_at_commit`: an applicable satisfying release has a recorded time at or before the commit boundary.
2. `hallucinated_dependency`: the available evidence supports package no-match, requested-version no-match, or a satisfying release dated after the boundary. The subtype and time difference are retained.
3. `insufficient_evidence`: identity, syntax, retrieval, timestamp or history coverage prevents the decision; the specific reason is retained.
4. `out_of_scope`: explicit standard-library, local, first-party or other scope exclusion.

The hallucinated-dependency indicator assumes that the selected public registry is the intended source and that unobserved deletion, migration or historical omission has not removed a satisfying release. Package no-match concerns a potentially hallucinated package name. Version no-match concerns a potentially hallucinated version or invalid request. Saved evidence is applied to all queryable episodes using the same classification rules.

## Current results

| Final label | All episodes | Within queryable |
| --- | --- | --- |
| present_at_commit | 1,256,976 | 1,256,976 |
| hallucinated_dependency | 60,516 | 60,516 |
| insufficient_evidence | 291,918 | 16,400 |
| out_of_scope | 70,375 | 1,241 |

| Hallucinated subtype | Episodes |
| --- | --- |
| package_no_match | 56,235 |
| postdate | 2,938 |
| version_no_match | 1,343 |

For the 2,938 `postdate` episodes, the interval is the recorded matching-release time minus committer time. The groups below do not overlap; upper boundaries are inclusive. Percentages use all postdate episodes as the denominator and retain the main analysis's timestamp proxies.

| Release time after commit | Episodes | Share of postdate |
| --- | ---: | ---: |
| 0–2 hours | 299 | 10.18% |
| >2–24 hours | 312 | 10.62% |
| >24 hours–7 days | 600 | 20.42% |
| >7–30 days | 546 | 18.58% |
| >30 days | 1,181 | 40.20% |

Cumulative coverage: within 2 hours: 299 (10.18%); within 24 hours: 611 (20.80%); within 7 days: 1,211 (41.22%); within 30 days: 1,757 (59.80%). These intervals describe the time differences and do not change the final labels.

| Ecosystem | Queryable | present_at_commit | hallucinated_dependency | insufficient_evidence | out_of_scope | H / queryable |
| --- | --- | --- | --- | --- | --- | --- |
| Cargo | 47,648 | 37,460 | 7,738 | 2,225 | 225 | 16.2399% |
| Go | 53,813 | 45,428 | 7,240 | 1,145 | 0 | 13.4540% |
| Maven | 48,985 | 45,823 | 1,211 | 1,939 | 12 | 2.4722% |
| PyPI | 322,919 | 286,804 | 29,613 | 5,498 | 1,004 | 9.1704% |
| npm | 861,768 | 841,461 | 14,714 | 5,593 | 0 | 1.7074% |

Agent and agent-by-ecosystem tables, multi-agent overlap and queryable repository counts are in `results_paper_tables/`. They use every qualifying agent attribution, counting each episode once per agent and once globally.

## Change from the previous combined result

The previous Java-repaired result had 1,747,439 episodes and 1,388,441 queryable episodes. This run rebuilds all ecosystems after manifest extraction, syntax and Java mapping corrections. Identity aggregation can add, replace or merge episodes, so changes in counts include denominator changes as well as classification changes.

| Label | Previous Java-repaired run | Current integrated run |
| --- | --- | --- |
| present_at_commit | 1,308,899 | 1,256,976 |
| hallucinated_dependency | 60,992 | 60,516 |
| insufficient_evidence | 308,269 | 291,918 |
| out_of_scope | 69,279 | 70,375 |

Episode additions, replacements and merges are recorded in `identity_transitions.csv`.

## Time evidence and sensitivity

Both author and committer dates are available for all commits. The main analysis uses **committer time**; author time is used for a sensitivity check.

The previous run used author time for most commits because it had loaded only part of the saved committer metadata. After recovering the remaining dates, Stage3 was rerun with the same episodes and registry evidence. This changes 6 episode labels.

Author time is evaluated separately using the same registry observations. It changes 59 labels relative to the committer-based main result:

| Committer outcome | Author outcome | Episodes |
| --- | --- | --- |
| postdate | present_at_commit | 2 |
| present_at_commit | postdate | 57 |

Package/version no-match findings do not depend on a release-time comparison. The 24-hour interval describes postdate cases; it no longer separates Confirmed from Probable. Detailed comparisons are in `author_sensitivity.csv` and `time_update_transitions.csv`.

| Time-comparison label | Boundary | Episodes |
| --- | --- | --- |
| hallucinated_dependency | committer | 2,938 |
| present_at_commit | committer | 1,256,976 |

Google Maven HTTP Last-Modified, Maven index timestamps and Go module timestamps retain their source types. The same complete population is also classified with proxy timestamps withheld:

| Label with proxy timestamps withheld | Episodes |
| --- | --- |
| present_at_commit | 1,165,725 |
| hallucinated_dependency | 60,264 |
| insufficient_evidence | 383,421 |
| out_of_scope | 70,375 |

## Existing OSV results

The saved OSV analysis concerns the historical canonical population of **400,813 exact-version episodes**. Its scope is recorded separately from the current rebuilt denominator.

| OSV status at author time | Episodes | Share of historical exact episodes |
| --- | --- | --- |
| Published and active by author time | 30,444 | 7.5956% |
| Published after author time | 35,680 | 8.9019% |
| No matching advisory observed | 334,689 | 83.5025% |

These are episode counts, not distinct advisory documents. **18 episodes across 15 package identities** have an active malicious flag: 14 npm identities and one PyPI identity. Twelve episodes concern nine `com.unity.*` names stored as npm identities; their intended registry requires source validation. Among **480 historical current-absence episodes** with an advisory active at author time, the historical adjudication was 349 Not Hallucination, 65 Probable and 66 Indeterminate. Saved source tables are under `osv/`.

## Remaining evidence and deferred repairs

| Ecosystem | Reason | Episodes |
| --- | --- | --- |
| PyPI | upstream_identity_unresolved | 139,522 |
| Maven | upstream_identity_unresolved | 88,663 |
| npm | upstream_identity_unresolved | 44,900 |
| npm | unsupported_query_syntax | 5,274 |
| PyPI | matching_release_time_missing | 4,177 |
| Cargo | upstream_identity_unresolved | 2,417 |
| Cargo | registry_request_failed | 1,845 |
| Maven | unsupported_query_syntax | 1,346 |
| PyPI | unsupported_query_syntax | 1,164 |
| Go | empty_version_history | 924 |
| Maven | matching_release_time_missing | 502 |
| Cargo | unsupported_query_syntax | 275 |

Coverage remains limited where child snapshots are unavailable or unsupported manifest syntax remains. Unresolved catalog references and build variables require their originating configuration. Additional Python wheel mappings, ambiguous Java artifacts, Android/runtime identities, historical npm tags and missing registry timestamps require separate work. These items are retained with reasons for a later pass.

## Reproduction and verification

The code covers collection through final classification. The current classification can be reproduced offline from the supplied Stage1/2 tables, registry evidence and commit dates. Earlier extraction steps also need the upstream caches described in `README.md`. [DATA_ACCESS.md](DATA_ACCESS.md) explains the [downloads](https://drive.google.com/drive/folders/1Qp-xgDnHjzgSQWFrJgoIo_5FvHnEq7Ta?usp=sharing).

Regression tests and full-data checks passed, covering extraction, identity/version matching, classification and agreement between detailed records and summary counts.

Current results are in `results_integrated/`; the rebuilt Stage1/2 data are in `results_replay_v2/`. Earlier results are retained for comparison. Commands, file descriptions and checksums are provided with the download guide and manifests.

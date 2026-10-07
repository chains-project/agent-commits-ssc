"""Render the current integrated experiment in the existing supervisor report."""
import json
from collections import Counter
from final_report import BASE,LABELS,rows,table,result_tables
from postdate_summary import report_section


def postdate_section(directory):
    if not (directory/'postdate_intervals.csv').is_file():
        return ''
    return report_section(rows(directory,'postdate_intervals.csv'))


def commit_time_section(directory,summary):
    if 'commit_dates' not in summary:
        return 'This historical run used packaged committer dates with author fallback.'
    comparison=Counter()
    for r in rows(directory,'author_sensitivity.csv'):
        comparison[r['committer_label'],r['committer_subtype'],r['author_label'],r['author_subtype']]+=int(r['episodes'])
    changed=[(k,v) for k,v in comparison.items() if k[:2]!=k[2:]]
    difference=sum(v for k,v in comparison.items() if k[0]!=k[2])
    sensitivity=table(['Committer outcome','Author outcome','Episodes'],[
        [k[1] or k[0],k[3] or k[2],f'{v:,}'] for k,v in changed])
    receipt=directory/'time_update_verification.json'
    delta=''
    if receipt.is_file():
        checked=json.loads(receipt.read_text())
        delta=f" This changes {checked['counts']['label_changed']:,} episode labels."
    return f'''Both author and committer dates are available for all commits. The main analysis uses **committer time**; author time is used for a sensitivity check.

The previous run used author time for most commits because it had loaded only part of the saved committer metadata. After recovering the remaining dates, Stage3 was rerun with the same episodes and registry evidence.{delta}

Author time is evaluated separately using the same registry observations. It changes {difference:,} labels relative to the committer-based main result:

{sensitivity}

Package/version no-match findings do not depend on a release-time comparison. The 24-hour interval describes postdate cases; it no longer separates Confirmed from Probable. Detailed comparisons are in `author_sensitivity.csv` and `time_update_transitions.csv`.'''


def render(directory,summary,checks):
    overall,ecosystems,subtypes=result_tables(directory)
    stage=summary['stage12']['counts']
    inputs=json.loads((BASE/'inputs/replay.manifest.json').read_text())['counts']
    inspected=inputs['patches']
    before=json.loads((BASE/'results_final/summary.json').read_text())
    changes=table(['Label','Previous Java-repaired run','Current integrated run'],
        [[k,f'{before["labels"][k]:,}',f'{summary["labels"][k]:,}'] for k in LABELS])
    coverage=table(['Manifest replay measure','Count'],[[k,f'{stage.get(k,0):,}'] for k in
        ['changed_manifest_files','snapshot_applied','manifest_snapshot_not_indexed','snapshot_cache_missing',
         'snapshot_line_mismatch','snapshot_text_mismatch','snapshot_parse_failed','non_research_path']])
    insufficient=sorted((r for r in rows(directory,'reasons.csv') if r['label']=='insufficient_evidence'),
        key=lambda r:int(r['episodes']),reverse=True)
    reasons=table(['Ecosystem','Reason','Episodes'],[[r['ecosystem'],r['reason'],f'{int(r["episodes"]):,}'] for r in insufficient[:12]])
    timing=table(['Time-comparison label','Boundary','Episodes'],[[r['label'],r['boundary_source'],f'{int(r["episodes"]):,}'] for r in rows(directory,'time_boundaries.csv')])
    proxies=Counter()
    for row in rows(directory,'proxy_sensitivity.csv'):proxies[row['label']]+=int(row['episodes'])
    proxy_table=table(['Label with proxy timestamps withheld','Episodes'],[[k,f'{proxies[k]:,}'] for k in LABELS])
    catalog=json.loads((BASE/'scripts/hallucination/java_class_catalog.json').read_text())
    from stage2_java_identity import catalog as lookup
    targets=lookup();artifacts=len({r['coordinate'] for r in targets.values()})
    java=json.loads((BASE/'results_java_full/summary.json').read_text())
    maven=[r for r in rows(directory,'label_counts.csv') if r['stratum']=='Maven']
    java_stages=table(['Java/Maven stage','All episodes','Queryable','Insufficient evidence'],[
        ['Before identity repair',f'{java["old_episodes"]:,}',f'{java["old_queryable"]:,}',f'{java["old_labels"]["insufficient_evidence"]:,}'],
        ['Previous Java-only repair',f'{java["episodes"]:,}',f'{java["queryable"]:,}',f'{java["labels"]["insufficient_evidence"]:,}'],
        ['Current integrated run',f'{sum(int(r["episodes"]) for r in maven):,}',
         f'{sum(int(r["episodes"]) for r in maven if r["queryable"]=="1"):,}',
         f'{sum(int(r["episodes"]) for r in maven if r["label"]=="insufficient_evidence"):,}']])
    return f'''# Agent dependency study: integrated results

Result version: `{summary.get('result_version','integrated_20261007')}`.

## Population and workflow

The current run contains **{summary['episodes']:,} dependency episodes**, including **{summary['queryable']:,} queryable episodes**, from **{stage['commits']:,} commits**. It rebuilds Stage2 across all six source languages and classifies every resulting episode using the saved registry evidence. Raw collection: [ASSERT-KTH/agent-commits-raw](https://huggingface.co/datasets/ASSERT-KTH/agent-commits-raw).

The workflow is collection and global commit deduplication → Stage1 added imports and manifest declarations → Stage2 identity/version alignment and episode aggregation → registry observations → uniform final classification. An episode key is commit, ecosystem, normalized dependency identity, query kind and query value. JS and TS references merge when that entire key agrees. Different version requests remain separate.

Queryable means an exact, range or name-only Stage2 request. The denominator uses the rebuilt episodes and the four categories below. Earlier canonical Confirmed/Probable counts use different rules and remain a separate historical result.

## Why extraction and alignment were rerun

The original manifest parser could miss additions when the diff hunk omitted a dependency-section heading. Full child manifests had already been fetched for some commits, but their structure was not used to restore all added declarations. Requirements continuations could also produce incomplete constraints. These affect event extraction and therefore both episode identities and denominators.

The repair uses saved child manifests to identify the structure around added lines. It also joins multiline requirements and handles comments correctly. Only added declarations become new dependency events; unchanged declarations supply context.

Saved snapshots and patches allowed inspection of {inspected:,} commits. Another 167,353 existing commits retain their previous extraction, including 40,880 with manifest events. Missing or mismatched snapshots also retain the previous extraction. The table below gives file-level coverage within the inspected subset. Existing import events and registry evidence are reused.

The revised extraction produced **{stage['events']:,} events**. It added {stage['events_added']:,} event records, removed {stage['events_removed']:,}, and changed events in {stage['events_changed_commits']:,} commits. It also recovered dependency events from {stage['new_commits']:,} additional commits.

{coverage}

## Ecosystem and identity repairs

Java now uses a catalog of **{len(targets):,} exact import targets across {artifacts} Maven artifacts**, supported by official documentation and source code. This connects recognized imports to queryable artifact coordinates. A nearby declaration for that artifact supplies the version when available; otherwise the query uses its name. Local and first-party references remain excluded.

{java_stages}

These are episode counts after each stage's own aggregation. The reduction reflects both recovered identities and merged references.

Version handling was also corrected. Python wildcard and supported Poetry constraints are interpreted as ranges. Maven literal versions use exact matching, and resolved property values are preserved. Lockfile context applies only to the corresponding manifest and package, preventing one subproject's version from overriding another's declaration. Go keeps its module-context matching and local-module exclusions. Stage2 then combines the events across all six languages.

Property-context corrections affected {summary['stage12'].get('context_repair',{}).get('repaired_commits',0):,} commits, and manifest-context matching corrections affected {summary['stage12'].get('scope_repair',{}).get('repaired_commits',0):,}; these groups may overlap. Examples are saved in `results_remaining_review/`.

Existing local and standard-library exclusions are retained where their supporting events still apply. Changed references are reassessed. Matching follows each ecosystem's version syntax; supported forms and library versions are listed in `README.md`.

## Final classification

1. `present_at_commit`: an applicable satisfying release has a recorded time at or before the commit boundary.
2. `hallucinated_dependency`: the available evidence supports package no-match, requested-version no-match, or a satisfying release dated after the boundary. The subtype and time difference are retained.
3. `insufficient_evidence`: identity, syntax, retrieval, timestamp or history coverage prevents the decision; the specific reason is retained.
4. `out_of_scope`: explicit standard-library, local, first-party or other scope exclusion.

The hallucinated-dependency indicator assumes that the selected public registry is the intended source and that unobserved deletion, migration or historical omission has not removed a satisfying release. Package no-match concerns a potentially hallucinated package name. Version no-match concerns a potentially hallucinated version or invalid request. Saved evidence is applied to all queryable episodes using the same classification rules.

## Current results

{overall}

{subtypes}

{postdate_section(directory)}

{ecosystems}

Agent and agent-by-ecosystem tables, multi-agent overlap and queryable repository counts are in `results_paper_tables/`. They use every qualifying agent attribution, counting each episode once per agent and once globally.

## Change from the previous combined result

The previous Java-repaired result had {before['episodes']:,} episodes and {before['queryable']:,} queryable episodes. This run rebuilds all ecosystems after manifest extraction, syntax and Java mapping corrections. Identity aggregation can add, replace or merge episodes, so changes in counts include denominator changes as well as classification changes.

{changes}

Episode additions, replacements and merges are recorded in `identity_transitions.csv`.

## Time evidence and sensitivity

{commit_time_section(directory,summary)}

{timing}

Google Maven HTTP Last-Modified, Maven index timestamps and Go module timestamps retain their source types. The same complete population is also classified with proxy timestamps withheld:

{proxy_table}

## Existing OSV results

The saved OSV analysis concerns the historical canonical population of **400,813 exact-version episodes**. Its scope is recorded separately from the current rebuilt denominator.

| OSV status at author time | Episodes | Share of historical exact episodes |
| --- | --- | --- |
| Published and active by author time | 30,444 | 7.5956% |
| Published after author time | 35,680 | 8.9019% |
| No matching advisory observed | 334,689 | 83.5025% |

These are episode counts, not distinct advisory documents. **18 episodes across 15 package identities** have an active malicious flag: 14 npm identities and one PyPI identity. Twelve episodes concern nine `com.unity.*` names stored as npm identities; their intended registry requires source validation. Among **480 historical current-absence episodes** with an advisory active at author time, the historical adjudication was 349 Not Hallucination, 65 Probable and 66 Indeterminate. Saved source tables are under `osv/`.

## Remaining evidence and deferred repairs

{reasons}

Coverage remains limited where child snapshots are unavailable or unsupported manifest syntax remains. Unresolved catalog references and build variables require their originating configuration. Additional Python wheel mappings, ambiguous Java artifacts, Android/runtime identities, historical npm tags and missing registry timestamps require separate work. These items are retained with reasons for a later pass.

## Reproduction and verification

The code covers collection through final classification. The current classification can be reproduced offline from the supplied Stage1/2 tables, registry evidence and commit dates. Earlier extraction steps also need the upstream caches described in `README.md`. [DATA_ACCESS.md](DATA_ACCESS.md) explains the [downloads](https://drive.google.com/drive/folders/1Qp-xgDnHjzgSQWFrJgoIo_5FvHnEq7Ta?usp=sharing).

Regression tests and full-data checks passed, covering extraction, identity/version matching, classification and agreement between detailed records and summary counts.

Current results are in `{directory.name}/`; the rebuilt Stage1/2 data are in `{Path(summary['stage12_path']).name}/`. Earlier results are retained for comparison. Commands, file descriptions and checksums are provided with the download guide and manifests.
'''


from pathlib import Path

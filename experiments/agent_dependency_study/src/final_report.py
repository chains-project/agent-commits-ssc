"""Render the single supervisor report from verified final combined results."""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
LABELS=['present_at_commit','hallucinated_dependency','insufficient_evidence','out_of_scope']


def rows(directory,name):
    with (directory/name).open(encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def table(headers,data):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |',
        *['| '+' | '.join(str(x).replace('|','\\|') for x in row)+' |' for row in data]])


def result_tables(directory):
    counts=rows(directory,'label_counts.csv')
    def n(scope,label,q=None):
        return sum(int(r['episodes']) for r in counts if r['stratum']==scope and r['label']==label and (q is None or r['queryable']==q))
    overall=table(['Final label','All episodes','Within queryable'],[[k,f'{n("global",k):,}',f'{n("global",k,"1"):,}'] for k in LABELS])
    data=[]
    for eco in sorted({r['stratum'] for r in counts}-{'global'}):
        denominator=sum(n(eco,k,'1') for k in LABELS)
        data.append([eco,f'{denominator:,}',*[f'{n(eco,k,"1"):,}' for k in LABELS],f'{100*n(eco,"hallucinated_dependency","1")/denominator:.4f}%'])
    ecosystem=table(['Ecosystem','Queryable',*LABELS,'H / queryable'],data)
    subtypes=Counter()
    for r in counts:
        if r['stratum']=='global' and r['subtype']:
            subtypes[r['subtype']]+=int(r['episodes'])
    return overall,ecosystem,table(['Hallucinated subtype','Episodes'],[[k,f'{v:,}'] for k,v in sorted(subtypes.items())])


def supporting_tables(directory,summary):
    java=summary['java']
    old=json.loads((BASE/'results/summary.json').read_text())
    changes=table(['Label','Before Java repair','Final combined'],[[k,f'{old["labels"][k]:,}',f'{summary["labels"][k]:,}'] for k in LABELS])
    timing=table(['Time-comparison label','Boundary','Episodes'],[[r['label'],r['boundary_source'],f'{int(r["episodes"]):,}'] for r in rows(directory,'time_boundaries.csv')])
    proxies=Counter()
    for row in rows(directory,'proxy_sensitivity.csv'):
        proxies[row['label']]+=int(row['episodes'])
    proxy_table=table(['Label with proxy times withheld','Episodes'],[[k,f'{proxies[k]:,}'] for k in LABELS])
    insufficient=sorted((r for r in rows(directory,'reasons.csv') if r['label']=='insufficient_evidence'),key=lambda r:int(r['episodes']),reverse=True)
    reasons=table(['Ecosystem','Remaining reason','Episodes'],[[r['ecosystem'],r['reason'],f'{int(r["episodes"]):,}'] for r in insufficient[:12]])
    java_changes=table(['Java outcome','Before','After'],[[k,f'{java["old_labels"].get(k,0):,}',f'{java["labels"].get(k,0):,}'] for k in LABELS])
    return changes,timing,proxy_table,reasons,java_changes


def render(directory,summary,checks):
    overall,ecosystems,subtypes=result_tables(directory)
    changes,timing,proxy_table,reasons,java_changes=supporting_tables(directory,summary)
    java=summary['java']
    return f'''# Agent dependency study: final unified results

## Population and workflow

The final analysis contains **{summary['episodes']:,} dependency episodes**, including **{summary['queryable']:,} queryable episodes**. It combines the repaired Java pipeline with the completed npm, PyPI, Cargo and Go results. Raw commit data: [ASSERT-KTH/agent-commits-raw](https://huggingface.co/datasets/ASSERT-KTH/agent-commits-raw).

The workflow is commit collection/deduplication → Stage1 added imports and manifest references → Stage2 dependency identity, version request and episode aggregation → saved registry evidence → uniform Stage3 classification. References with the same commit, ecosystem, normalized identity and query kind/value form one episode. Different version requests retain separate episodes.

Queryable denotes a Stage2 exact-version, range or name-only request. It includes requests subsequently assigned an evidence gap or contextual exclusion. Java queryability is rebuilt through Stage2 deduplication. Source hashes are in `inputs/manifest.json` and `inputs/java_stage2.manifest.json`.

## Identity and ecosystem handling

Java alignment uses manifest/context evidence and a catalog of **{java['catalog_targets']} exact import targets across {java['catalog_artifacts']} artifacts**, covering 97 of the 100 most frequent unresolved Java target strings. Entries identify classes, selected static imports and explicitly listed package wildcards using official documentation and tagged source trees.

For catalog hits, the resolver uses the version from the nearest ancestor declaration for the same coordinate, or a name-only query when no version is available. Conflicting declarations retain their individual queries and a name-only import query. Local/first-party classifications take precedence. Mapping witnesses preserve the source, rule, declaration and resulting episode IDs. Other imports use the existing contextual alignment rules; Stage1 extraction is preserved.

npm uses semver 7.6.3; PyPI uses packaging 21.3. Cargo supports caret/tilde/wildcards and intersected comparators. Maven uses literal exact matching and numeric interval ranges. Go uses normalized module-version matching.

## Final classification rule

1. Identify standard-library, local and other excluded references. Unresolved identities retain a specific evidence-gap reason.
2. Match the package/version request against applicable saved registry observations. A satisfying release dated at or before the boundary is `present_at_commit`.
3. Classify `hallucinated_dependency` for supported package no-match, version no-match or postdate findings, preserving the subtype and time difference.
4. Use `insufficient_evidence` for unresolved identity, unsupported syntax, failed requests, missing times or insufficient history coverage. `out_of_scope` records explicit scope exclusions.

The hallucinated-dependency indicator assumes that the selected public registries represent the intended source and that unobserved deletion, migration or historical omissions have not removed a satisfying release. Package no-match concerns a potentially hallucinated package name; version no-match concerns a potentially hallucinated version or invalid version request.

The rerun uses saved registry evidence. Official Java source research supplies class-to-artifact mappings; registry version and timing evidence determine final categories.

## Final results

{overall}

{subtypes}

{ecosystems}

## Effect of the Java repair

The rerun addresses an identity-resolution gap before registry classification. Java import namespaces do not directly identify Maven artifacts. The earlier alignment left many recognizable external classes unresolved and could associate an import with an unrelated artifact when only one new Maven coordinate was present in its scope. These unresolved identities prevented valid registry queries.

The repair inserts the sourced class-to-artifact catalog before that fallback. For example, `lombok.Data` maps to `org.projectlombok:lombok`; a version is attached only from the nearest applicable declaration for that same coordinate. The resolver retains local exclusions, preserves conflicting requests and records the mapping evidence. Stage2 then rebuilds episode identities and deduplicates references, after which the same Stage3 classifier evaluates the saved registry records.

All **{java['commits']:,} Java commits** and **{java['events']:,} saved Stage1 events** were processed through the repaired Stage2 and Stage3. This is necessary because corrected identities can merge or split episodes and change the queryable denominator. Java episodes changed from **{java['old_episodes']:,} to {java['episodes']:,}**, and queryable episodes from **{java['old_queryable']:,} to {java['queryable']:,}**.

{java_changes}

Two newly resolved queries receive `version_no_match`: `org.springframework.security:spring-security-core:7.0.5` and `org.springframework:spring-beans:6.2.17`. Their declared requests have no literal match in the saved successful version enumerations. `results_java_full/new_positive_evidence.json` links both to mapping witnesses and registry records. The Java hallucinated total changes from 1,200 to 1,199 after all identity merges and corrections.

**{java['recovered_old_unresolved']:,} old unresolved episode IDs** acquired queryable identities. Event bridges distinguish recovery from merging and splitting. The former global population was 1,769,653 episodes with 1,364,856 queryable; the final tables use the rebuilt population.

{changes}

The combined output replaces the entire Maven slice. The other **{checks['non_maven_rows_identical']:,} records** match the completed baseline exactly.

## Time boundary and proxy sensitivity

The classifier uses the packaged committer date where available, with a recorded author-time fallback. An earlier author boundary can increase temporal positives. Package/version no-match uses the absence assumptions; present/postdate outcomes use publication-time comparisons.

{timing}

Go module time, Maven index time, HTTP Last-Modified and unspecified legacy timestamps are recorded as publication-time proxies. With these times withheld while retaining version observations:

{proxy_table}

The 24-hour difference remains a descriptive sensitivity field in `positive_sensitivity.csv`.

## Existing OSV results

The saved advisory analysis covers **400,813 exact-version episodes in the frozen canonical population**, using author time and cached evidence. These are the original advisory-coverage counts; the Java repair above updates dependency-registry classification.

| Advisory timing | Exact-version episodes | Share of these exact episodes |
| --- | --- | --- |
| Published and active by author time | 30,444 | 7.5956% |
| Published after author time | 35,680 | 8.9019% |
| No matching advisory observed | 334,689 | 83.5025% |

Counts represent episodes rather than distinct advisory documents. **18 episodes across 15 ecosystem/package identities** have an active malicious flag: 14 npm identities and one PyPI identity. Twelve episodes concern nine `com.unity.*` names stored as npm identities; their intended registry requires source validation.

For **480 current-absence episodes**, an advisory was already active at author time. Their historical adjudication outcomes were 349 Not Hallucination, 65 Probable and 66 Indeterminate. These annotations provide a separate source of historical context. Saved source tables are in `osv/`.

## Remaining evidence gaps

{reasons}

Three top-100 targets need separate handling: Android platform `Context` and two Swagger targets with ordinary/Jakarta artifact variants. Less frequent imports retain their current mapping evidence. Other remaining work concerns full-manifest extraction, local/runtime identity resolution, version parsing and registry timestamps. Each episode retains its specific reason.

The follow-up inventory identifies concrete repair opportunities. Saved official Java source trees contain unique coordinate candidates for another 1,009 class targets, covering 16,162 target–episode associations before deduplication. The query-syntax inventory includes 3,039 npm catalog references, 1,255 Maven/Gradle dollar-variable requests, and Python requests with caret/tilde constraints (764), trailing continuation characters (719), or wildcards incorrectly classified as exact versions (165). These are diagnostic groups; final recovery depends on source context and identity aggregation. The wildcard classification and continuation handling require parser corrections.

Catalog references need expansion from their originating configuration, as described by [pnpm catalogs](https://pnpm.io/catalogs) and [Gradle version catalogs](https://docs.gradle.org/current/userguide/version_catalogs.html). Python constraints require their originating syntax, including [Poetry constraints](https://python-poetry.org/docs/dependency-specification/) and [PEP 440 version specifiers](https://packaging.python.org/en/latest/specifications/version-specifiers/). The current result tables precede these additional repairs. Diagnostic counts and candidates are under `results_remaining_review/`.

## Reproduction and verification

The delivery includes collection, corpus merge, metadata/diff retrieval, Stage1, Stage2/context, registry collection and final classification. Full TOML context rebuilding uses Python 3.11+; saved-input runs were tested on Python 3.9.12 and Node 23.7.0.

Forty tests cover matching, scope, module isolation, conflicts, aggregation, missing evidence and proxy handling. Independent verification checks all Java links and bridges, final label/time invariants, global totals, hashes and exact non-Maven reuse. `source_preservation.json` records the original copy and Java amendment; `package_manifest.json` fingerprints the delivery.

Use the final workflow in `README.md`. Current outputs are `{directory.name}/episodes.csv.gz`, its summary tables, and `results_java_full/` for Java bridges and mapping witnesses. Baseline files remain under `results/` for comparison.
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results',type=Path,default=BASE/'results_integrated')
    args=parser.parse_args()
    summary=json.loads((args.results/'summary.json').read_text())
    checks=json.loads((args.results/'verification.json').read_text())
    assert checks['status']=='passed'
    renderer=render
    if 'stage12' in summary:
        from integrated_report import render as renderer
    (BASE/'REPORT.md').write_text(renderer(args.results,summary,checks),encoding='utf-8')
    print(BASE/'REPORT.md')

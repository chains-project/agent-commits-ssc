# RQ2 method

RQ2 studies hallucinated dependencies in AI coding-agent commits across TypeScript, Python, JavaScript, Rust, Go, and Java.

Stage 1 parses unified patches and extracts external import and direct manifest events. Stage 2 links observations into dependency episodes and resolves manifest, lockfile, workspace, alias, and version context. Stage 3 collects or applies registry and advisory evidence using author time as the temporal boundary.

The final adjudicator combines candidate, context, registry, temporal, mapping, and patch evidence into four conservative labels: confirmed hallucination, probable hallucination, not hallucination, and indeterminate. A separate guard corrects rows whose apparent manifest evidence occurs only in comments.

Current registry absence alone is not proof of hallucination. Missing historical evidence, local/private specifications, mapping uncertainty, and unresolved version semantics remain non-conclusive.

## Public result

The final adjudication table has 74,618 candidate rows. It contains 322 confirmed, 382 probable, 21,821 not hallucination, and 52,093 indeterminate labels. The six-language canonical experiment was not rerun for the clean export.

Historical base and v2-v6 repair wrappers are provenance, not separate experimental versions. The public versionless module consolidates their final effective behavior. The recorded nine-case comparison found no field mismatches between the prior v6 chain and the consolidated module.

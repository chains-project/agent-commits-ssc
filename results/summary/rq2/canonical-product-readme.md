# RQ2 targeted PEP 508 six-language v2

This versioned data product is the completed targeted repair of the six-language
Hallucinated Dependencies analysis. Python Stages 1-3 were regenerated with the
correct PEP 508 environment-marker parser. Hash-verified products for the other
five languages were reused, including the audited Java repair v3 product.

## Final result

- Queryable unique episode denominator: 1,364,856
- Candidate cases: 74,618
- Confirmed hallucination: 322
- Probable hallucination: 382
- Not hallucination: 21,821
- Indeterminate: 52,093
- Strict episode prevalence: 0.024% (95% Wilson CI 0.021-0.026%)
- Confirmed-plus-probable sensitivity: 0.052% (95% Wilson CI 0.048-0.056%)

These are conservative operational labels for dependency inconsistency in
observed commits. They do not identify a causal model effect, and present-day
registry absence is not treated as proof of hallucination.

## Entry points

- Final adjudication: `data/rq2/v2_native_adjudication.csv`
- Scientific summary: `results/summary/rq2/v2_final_summary.json`
- Language summary: `results/summary/rq2/v2_final_by_language.csv`
- Final verification: `results/validation/rq2/final_analysis_verification.json`
- Canonical source hashes: `data/rq2/canonical_table_index.json`
- Public product manifest: `data/rq2/public_product_manifest.json`

The clean export excludes the non-final report surface. `data/rq2/experiment_complete.json`
records the public closure boundary without rerunning the canonical experiment.

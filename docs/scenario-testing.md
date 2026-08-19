# Scenario testing

Tests and validation answer different questions. Unit and regression tests
check code behavior. Scenario tests run controlled synthetic inputs through
multiple real pipeline stages and compare the outputs with a predefined
Oracle. Final-analysis verification checks the completed data product.

## Stage 4

The Stage 4 suite contains 49 supported scenarios. It materializes synthetic
patch and population inputs, executes the actual Stage 1-3 pipeline offline,
and compares the four canonical table outputs and captured branches with
expected values. All 49 pass. The removed Java BOM case is outside the public
scope and is not included in the fixture or denominator.

## Final adjudication

Nine additional scenarios run Stage 1, Stage 2, Stage 3, the versionless final
adjudicator, and the independent comment guard. They cover six languages and
all four final labels: two confirmed, two probable, four not hallucination, and
one indeterminate. All nine pass.

The Maven vendor scenario uses a fixed verified sidecar, so the suite does not
depend on live registry access. The runner emits JSON only and does not write
canonical experiment CSV files.

## Other validation records

The equivalence JSON records nine legacy/current field comparisons with no
mismatches. The final-analysis record contains 80 passed consistency checks
covering run state, hashes, table continuity, schema invariants, label counts,
PEP 508 repairs, and recomputed statistics. None of these layers is a human
gold-standard accuracy evaluation.

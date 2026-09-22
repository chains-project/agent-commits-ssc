# Can public commits identify the underlying model?

## Question and findings

This exploration asked whether GitHub artifacts can identify the model that actually executed a coding task, beyond identifying the coding harness. A harness name, a declared model and an execution record provide different kinds of evidence.

The exploration identified reproducible signals at two levels. A producer-documented Qwen signature matched 24 commits in the frozen expanded corpus. In a separate stratified 120-case sample, 42 cases contained a model declaration or structured record under the frozen codebook: 39 cases with an exact-model token and three family-only declarations.

The results show where model information is observable: all 42 qualifying cases cite commit messages or trailers. The package provides the signature rules, sampling and coding methods, and compact tables needed to recalculate the descriptive results. The distinction between these declarations and actual model execution is discussed below.

## Data and workflow

Two related analyses used different frames:

| Analysis | Frozen frame | Purpose |
|---|---|---|
| Harness signatures and keyword discovery | 1,852,606 unique repository-SHA commits in the expanded frame | Check producer-documented signatures and the contexts of model-name matches |
| Underlying-model evidence | 460,207 unique repository-SHA commits in the matched frame | Inspect whether commit-linked evidence identifies the executing model |

The expanded frame deduplicates the matched collection and a supplementary branch of 1,451,545 commits. The collection was assembled through existing coding-agent attribution channels and does not represent all GitHub activity. Full messages, patches and official-source snapshots remain external.

The workflow was to freeze inputs and rules, scan public message signals, select bounded samples, code the available evidence, and check whether local diff/config artifacts added model-binding information. Coding used explicit artifact content rather than coding style or repository topic; generic family trailers are identified separately below.

## A. Harness signatures and keyword discovery

The official-source audit checked whether products emit a documented attribution mechanism. The source revisions below are pinned to the inspected versions.

| Family | Official source revision | Finding in the bounded audit |
|---|---|---|
| Qwen | [Qwen Code](https://github.com/QwenLM/qwen-code/tree/055e831556ed9772664dcedb23c6e647c478fc90) | Implements the exact Qwen-Coder co-author trailer for supported inline commit paths |
| DeepSeek | [DeepSeek-Coder](https://github.com/deepseek-ai/DeepSeek-Coder/tree/2f9fd85927c669dae3c0fbb2d607274023af243e) | No producer-controlled commit-attribution mechanism found |
| Kimi | [Kimi Code](https://github.com/MoonshotAI/kimi-code/tree/052e98ec17ac8d931873ef892fb0e1912aa401e0) | Repository policy rejects co-author attribution; no product-wide positive signature found |
| GLM/CodeGeeX | [CodeGeeX extension](https://github.com/CodeGeeX/codegeex-vscode-extension/tree/3bbc5305bae91928152eb76ef41c4b822edb3c56) | Relevant wording concerned generated-code UI, not commit construction |
| Doubao/Seed/TRAE | [TRAE Agent](https://github.com/bytedance/trae-agent/tree/e839e559ac61bdd0e057c375dd1dee391fee797d) | Commit example was generic contributor guidance |

The exact `Co-authored-by: Qwen-Coder <qwen-coder@alibabacloud.com>` trailer matched 24 unique repository-SHA commits: one in the matched frame and 23 in the supplementary branch. This connects a documented attribution mechanism to observable signatures in the selected corpus.

A broader scan found 2,724 Qwen, 2,415 DeepSeek, 699 Kimi, 442 GLM and 268 Doubao/Seed/TRAE name or context matches. A deterministic balanced sample selected 48 contexts per family, 240 in total, from 6,651 eligible branch-repository-family contexts. Agent-assisted discovery annotation assigned:

| Context | Cases |
|---|---:|
| API, model configuration, dependency or integration | 139 |
| Operational attribution wording | 49 |
| Documentation, benchmark, test or release | 20 |
| Ordinary discussion or unrelated term | 14 |
| Insufficient context | 8 |
| Ambiguous | 5 |
| Structured harness-model self-report | 5 |

The context sample distinguishes operational attribution from the more common API/configuration/integration references: 139 of the 240 selected contexts fell in the latter group. This provides a practical filter for interpreting keyword matches. The final disposition retained the existing Qwen rule, accepted no registry additions (`accepted_additions=[]`), and left the RQ1 population unchanged.

## B. Underlying-model evidence

The matched frame was divided into message-signal strata: L3, a model string with a direct binding cue (359,625 commits); L2, a structured/configuration cue (56); L1, name mention only (26,020); and L0, no lexicon signal (74,506). These are sampling categories, not accepted model attributions.

A two-stage probability sample selected 30 commits per stratum, through developer clusters and then commits. Two agents independently coded the 120 cases using the frozen [codebook](model-observability-codebook.md). Five fields determined agreement: evidence level, granularity, provider, family and exact-model token. The compact data preserve the resulting labels and sampling fields.

| Evidence label | Cases | Meaning |
|---|---:|---|
| X3 | 34 | Direct named-model self-report tied to the commit |
| X2 | 8 | Commit-proximate structured model record without execution attestation |
| X0 | 72 | No usable model information under the codebook |
| XC | 6 | Retained as uncertain under the frozen five-field agreement rule |

Among the 42 X2/X3 cases, 39 stated an exact-model token and three only a family. Their family labels were Claude in 36 cases and GPT in six. The three family-only cases use generic Claude co-author trailers, which do not independently distinguish the harness from its underlying model. The six XC cases include classification and model-token differences; one was already XC for both coders.

Both sets of coding refer only to messages or trailers for all 42 qualifying cases. The structured records were also observed in messages. The exported source-category field follows coder A, as specified in the codebook.

All 120 message locations were recovered. The frozen local index matched 116 cases: 109 were marked `ok` and seven had errors; four cases were absent from the index. Bounded inspection of the available diff/config material added no accepted model-binding label beyond the message/trailer evidence.

## Interpretation

The study provides a repeatable way to separate harness signatures, model declarations and ordinary model-name references. It identifies explicit model tokens in commit-linked text and shows which evidence sources support those labels. The registry and codebook make these distinctions inspectable, while the compact sample supports exact recounting.

These are signature and declaration findings within selected frozen material, not verified execution attribution or model-use rates. The 24 Qwen matches are not a total of Qwen use, and neither balanced discovery counts nor the stratified sample describe market share. Declarations and the bounded diff coverage did not establish which model actually executed the coding task; that question requires corroborating task-linked execution evidence. This finding does not rule out attribution with richer data. No new signature entered production or changed confirmatory RQ1/RQ2 results.

## Code, data and reproduction

The [five-slide presentation](presentations/harness-model-observability.pptx) illustrates the signature and keyword-discovery results. Read it with this report for the underlying-model feasibility finding.

From the repository root with Python 3.11 or newer:

```sh
python -m unittest discover -s tests -p 'test_*.py'
python scripts/exploration/model_observability/summarize_attribution_feasibility.py --root .
```

The second command reads the two compact 120-case tables under `data/model_observability/`, recalculates label, granularity, evidence-source, stratum and frozen index-status counts, and checks exact equality with `EXPLORATORY_ATTRIBUTION_FEASIBILITY_SUMMARY.json` under `results/model_observability/`.

The same results directory retains the final keyword, discovery-annotation and Qwen-signature counts. Scanner, sampling, bounded-context and consensus code is under `scripts/exploration/model_observability/`, alongside the registries and lexicons. The [artifact catalog](artifact-catalog.md#model-observability-supplement) lists each file's role and the external inputs required by the original analysis runners.

Full scans and reinspection require separately held population, message, diff and coding inputs, plus the frozen input manifest. The domestic scan also requires an explicit `--baseline-closure` record. These inputs have no asserted public download location. The compact tables retain the existing sample design and add evidence-source and availability fields without full messages, patches or local locators. See [data availability](data-availability.md). MIT covers original code and documentation, not third-party raw records.

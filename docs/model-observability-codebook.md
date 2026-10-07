# Exploratory Model Observability Codebook

Version: `2026-09-04-e0-v1`

Status: frozen before E1 census

Purpose: agent-coded exploratory observability only; not ground truth or actual-execution attribution.

## 1. Unit and evidence boundary

Reading the results: these frozen labels distinguish public declarations and
records. Even an exact-model token does not independently establish execution.
The accompanying feasibility report uses sample counts to describe the
available evidence; it does not promote X2/X3 to verified underlying-model
attribution.

The unit is one unique repository-SHA in the matched 460,207-commit frame. Coders receive a deidentified bounded commit-message context and, when locally available, bounded lines from that same commit's diff/config artifact. They must not browse, infer a real identity, or use general world knowledge about a developer.

Code what the public artifact says or structurally records. Do not decide which model “probably” wrote the code from style, quality, programming language, repository topic, or harness name alone.

## 2. X level

Choose exactly one:

- `X3`: the commit message explicitly self-reports that a named model/family was used for, generated, authored, implemented, or co-authored this commit/change. The model and binding cue must be in the same statement. A complete named-model `Co-authored-by` trailer qualifies as a direct self-report, not as verified execution.
- `X2`: commit-proximate structured metadata/config records a model, provider, or generator for the target commit/workflow, but does not explicitly attest actual execution. Examples include a model field in a same-commit agent/session artifact or a target-SHA git note. An application model setting changed by the commit does not qualify unless the artifact is clearly agent/session provenance.
- `X1`: only weak contextual information exists, such as a versioned harness default or a repository/developer statement not bound to the commit. E0-E3 local packages normally lack enough evidence for X1; do not infer it from the original harness channel.
- `X0`: no usable underlying-model information, or all model words concern dependencies, benchmarks, docs, tests, application configuration, inference workloads, examples, or unrelated content.
- `XC`: evidence conflicts, or multiple candidate models are present and cannot be bound to a single model/family for the target commit. Use XC instead of guessing.

The X level expresses public observability, not truth about execution. X3 is a stronger self-report than X2 but remains unverified text.

## 3. Identification granularity

Choose exactly one:

- `exact_model`: a version/model identifier is stated, such as `Claude Sonnet 4.5`, `gpt-5-codex`, or `qwen3-coder`.
- `model_family`: only a family such as Claude, Gemini, Qwen, or DeepSeek is identifiable.
- `provider_only`: only Anthropic, OpenAI, Google, Moonshot, Zhipu, or another provider is identifiable.
- `multi_model`: multiple models/families are explicitly tied to the work and cannot be reduced to one.
- `unknown`: no usable identification.

For `X0`, granularity must be `unknown`. For `XC`, use `multi_model` when multiplicity causes the conflict; otherwise use the most specific observed granularity and explain the conflict.

## 4. Labels

- `provider`: lowercase canonical provider or `unknown`; use semicolon-separated sorted values for multi-model cases.
- `model_family`: lowercase canonical family or `unknown`; use semicolon-separated sorted values for multi-model cases.
- `exact_model`: normalized observed token or `unknown`. Do not expand a family to a version.
- `source_type`: one or more of `commit_message`, `commit_trailer`, `same_commit_agent_config`, `same_commit_session_artifact`, `same_commit_other_config`, `diff_ordinary_code`, `none`.
- `binding_assumption`: `A1` for X3, `A2` for X2, `A3/A4` for X1, or `none` for X0/XC unless an observed conflict involves those assumptions.

## 5. Exclusions and hard cases

Assign `X0` when the commit merely:

- adds or updates a provider SDK, API endpoint, model dropdown, wrapper, benchmark, inference server, checkpoint, prompt fixture, test, documentation, or release note;
- reports that the software supports or evaluates a model;
- changes an application's default model without showing that an agent/session used it to create this commit;
- contains an original harness attribution but no underlying-model name;
- mentions a person, project, astrological Gemini, statistical GLM, or unrelated token that matches a lexicon entry.

Assign `XC` when the message says different models handled different parts but the target changes cannot be bound, when config and self-report disagree, or when a copied log/template contains multiple plausible model labels.

## 6. Agent independence and output

Each coder reads only the frozen input package and this codebook. Do not read the other coder's output. Provide one row per `selection_rank` with:

`selection_rank,x_level,granularity,provider,model_family,exact_model,source_type,binding_assumption,rationale`

Rationale must be no more than 240 characters and must cite only the bounded evidence. Do not include repository names, GitHub logins, emails, URLs, or full commit messages.

After initial coding, a frozen 12-case subset is recoded without consulting the first output. Recode stability describes within-agent process consistency.

## 7. Consensus rule

Exact agreement requires matching `x_level`, `granularity`, `provider`, `model_family`, and `exact_model`. If the two initial coders differ on any of these fields, consensus is `XC/uncertain`; no third agent adjudicates. This rule includes classification and model-token differences, not only conflicting evidence levels.

For agreeing rows, `source_type`, `binding_assumption` and `rationale` are copied from coder A; they are not part of the agreement test. The compact export retains `source_type`. Its 27 trailer-only, ten message-only and five combined positive cases therefore describe A's source field. Both coding files support the aggregate of 42 positives citing only messages/trailers.

The original runner also calculates agreement, X-level kappa and within-agent recode stability. Those process diagnostics are outside this concise results package.

## Exported coverage fields

The sample's `diff_status` and `diff_available` retain the frozen index-derived values. The summary's legacy key `readable_diffs` counts 109 rows marked `ok`; the compact replay does not reopen the external artifacts. The exported context runner checks that both JSON and patch files exist before accepting an indexed-`ok` row.

## 8. Reporting restrictions

- E1 full-frame rule counts are a signal census; they are not true model rates.
- E3 agent outputs are `agent-coded exploratory annotations`.
- Unknown and conflict remain in denominators and are never redistributed according to observed model composition.
- No E0-E3 output changes H/M/HM registry, RQ1 population, Methods, RQ2, or thesis confirmatory claims.

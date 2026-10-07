"""Apply the frozen codebook to fresh official evidence in blinded phases.

[IN]: blinded calibration/full reviewer form, official_evidence_matrix.csv,
frozen v1 codebook, and the independent interpretation addendum.
[OUT]: locked Codex calibration/full review CSV, invariants, summaries,
fingerprint manifests, and a blinded evidence report.
[POS]: Independent preliminary evidence coding. Never writes adjudicated labels
or edits either human reviewer form.
[SYNC]: If labels, precedence, thresholds, or output schemas change, update the
interpretation addendum, tests, scripts/OUTPUTS.md, and the active plan.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collect_codex_official_evidence import (
    DEFAULT_OUTPUT,
    REVIEW_ROOT,
    fingerprint,
    load_rows,
    write_json,
)


DECISION_FIELDS = (
    "agent_attribution_check", "mapping_check", "parent_child_check",
    "timestamp_check", "registry_check", "private_custom_local_check",
    "generated_vendored_check", "label", "reason", "confidence", "notes",
)
LEGAL_LABELS = {
    "confirmed_nonexistent_version_at_commit",
    "likely_nonexistent_version_at_commit",
    "false_positive_timestamp_or_history_rewrite",
    "false_positive_preexisting_dependency",
    "false_positive_first_party_monorepo",
    "false_positive_mapping",
    "false_positive_version_resolution",
    "false_positive_private_custom_or_local_registry",
    "false_positive_generated_or_vendored_context",
    "unverifiable_missing_historical_evidence",
    "exclude_not_agent_or_corrupt_source",
}
BOUNDARY_SECONDS = 15 * 60
REPORT_TEMPLATE = """# Codex independent blinded evidence review

## Scope

This is one Codex preliminary evidence review of {cases} hardened
history-validation candidates across {commits} commits. It is not a second
human coder, not adjudication, and not evidence of AI causality.

## Blinded result

{labels}

Positive labels only mean that official evidence supports the exact version
being unavailable while the target change introduced the candidate state.
Likely remains outside the conservative confirmed numerator.

## Evidence and sensitivity

Every case was refreshed from official GitHub commit/repository/raw endpoints
and the official npm packument. The frozen addendum treats a positive gap of
15 minutes or less as likely; exact gaps remain in the locked rows.

## Validity boundary

These cases are survivors of a historical strong-label regression, not a
random sample of agent commits. Formal thesis results still require blinded
human review, adjudication, and clustered case/commit reporting.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("calibration", "full"), required=True)
    parser.add_argument("--review-root", type=Path, default=REVIEW_ROOT)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def seconds_after(later: str, earlier: str) -> int | None:
    later_dt, earlier_dt = parse_iso(later), parse_iso(earlier)
    if later_dt is None or earlier_dt is None:
        return None
    return int((later_dt - earlier_dt).total_seconds())


def json_values(value: str) -> list[Any]:
    try:
        result = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return result if isinstance(result, list) else []


def is_true(value: str) -> bool:
    return value.strip().lower() == "true"


def agent_check(form: dict[str, str]) -> str:
    scope_ok = form.get("agent_evidence_scope") == "strict_agent_commit"
    channel_ok = bool(form.get("agent_evidence_channels", "").strip())
    return "pass" if scope_ok and channel_ok else "fail"


def mapping_check(form: dict[str, str]) -> str:
    mappings = json_values(form.get("mapping_statuses_json", ""))
    return "pass" if mappings == ["direct_mapping"] else "uncertain"


def timestamp_check(evidence: dict[str, str]) -> str:
    gap = publication_gap(evidence)
    if gap is None:
        return "uncertain"
    if gap <= 0:
        return "fail"
    if root_history_rewrite(evidence):
        return "fail"
    return "pass"


def publication_gap(evidence: dict[str, str]) -> int | None:
    return seconds_after(
        evidence.get("fresh_registry_publish_time", ""),
        evidence.get("fresh_committer_date", ""),
    )


def root_history_rewrite(evidence: dict[str, str]) -> bool:
    if evidence.get("fresh_parent_count") != "0":
        return False
    created = evidence.get("fresh_repo_created_at", "")
    committed = evidence.get("fresh_committer_date", "")
    gap = seconds_after(created, committed)
    return gap is not None and gap > 0


def state_value(state: dict[str, Any]) -> str:
    return str(state.get("version_spec") or state.get("resolved_version") or "")


def target_supported(states: list[Any], version: str) -> bool:
    return any(
        isinstance(state, dict) and version in state_value(state)
        for state in states
    )


def parent_child_check(form: dict[str, str], evidence: dict[str, str]) -> str:
    if evidence.get("fresh_parent_count") == "0":
        return "not_applicable"
    comparisons = json_values(evidence.get("fresh_parent_child_states_json", ""))
    states = {str(item.get("state", "")) for item in comparisons if isinstance(item, dict)}
    if "preexisting_same_spec_or_version" in states:
        return "fail"
    child = json_values(evidence.get("fresh_child_file_states_json", ""))
    parent = json_values(evidence.get("fresh_parent_file_states_json", ""))
    if target_supported(parent, form["version"]):
        return "fail"
    if target_supported(child, form["version"]):
        return "pass"
    return parent_fallback(form)


def parent_fallback(form: dict[str, str]) -> str:
    state = form.get("parent_child_state", "")
    if state == "preexisting_same_spec_or_version":
        return "fail"
    if state.startswith("package_added"):
        return "pass"
    return "uncertain"


def registry_check(evidence: dict[str, str]) -> str:
    complete = is_true(evidence.get("fresh_evidence_complete", ""))
    exists = is_true(evidence.get("fresh_version_exists", ""))
    return "pass" if complete and exists else "uncertain"


def private_check(form: dict[str, str]) -> str:
    return "fail" if is_true(form.get("non_registry_spec_hint", "")) else "pass"


def generated_check(form: dict[str, str]) -> str:
    return "fail" if is_true(form.get("generated_or_vendored_path_hint", "")) else "pass"


def resolution_supported(form: dict[str, str], evidence: dict[str, str]) -> bool:
    child = json_values(evidence.get("fresh_child_file_states_json", ""))
    if target_supported(child, form["version"]):
        return True
    values = json_values(form.get("version_specs_json", ""))
    values += json_values(form.get("resolved_versions_json", ""))
    return any(form["version"] in str(value) for value in values)


def all_checks(form: dict[str, str], evidence: dict[str, str]) -> dict[str, str]:
    return {
        "agent_attribution_check": agent_check(form),
        "mapping_check": mapping_check(form),
        "parent_child_check": parent_child_check(form, evidence),
        "timestamp_check": timestamp_check(evidence),
        "registry_check": registry_check(evidence),
        "private_custom_local_check": private_check(form),
        "generated_vendored_check": generated_check(form),
    }


def decide_label(form: dict[str, str], evidence: dict[str, str], checks: dict[str, str]) -> str:
    if checks["agent_attribution_check"] == "fail":
        return "exclude_not_agent_or_corrupt_source"
    if checks["timestamp_check"] == "fail":
        return "false_positive_timestamp_or_history_rewrite"
    if checks["parent_child_check"] == "fail":
        return "false_positive_preexisting_dependency"
    if checks["mapping_check"] != "pass":
        return "false_positive_mapping"
    if checks["private_custom_local_check"] == "fail":
        return "false_positive_private_custom_or_local_registry"
    if checks["generated_vendored_check"] == "fail":
        return "false_positive_generated_or_vendored_context"
    if checks["registry_check"] != "pass":
        return "unverifiable_missing_historical_evidence"
    if not resolution_supported(form, evidence):
        return "false_positive_version_resolution"
    return positive_label(evidence, checks)


def positive_label(evidence: dict[str, str], checks: dict[str, str]) -> str:
    if checks["parent_child_check"] == "not_applicable":
        return "unverifiable_missing_historical_evidence"
    gap = publication_gap(evidence)
    if gap is not None and gap <= BOUNDARY_SECONDS:
        return "likely_nonexistent_version_at_commit"
    return "confirmed_nonexistent_version_at_commit"


def evidence_grade(label: str) -> str:
    if label == "confirmed_nonexistent_version_at_commit":
        return "A"
    if label == "likely_nonexistent_version_at_commit":
        return "B"
    if label == "unverifiable_missing_historical_evidence":
        return "C"
    return "D"


def confidence(label: str) -> str:
    if label == "likely_nonexistent_version_at_commit":
        return "medium"
    if label == "unverifiable_missing_historical_evidence":
        return "low"
    return "high"


def reason_for(label: str, form: dict[str, str], evidence: dict[str, str]) -> str:
    gap = publication_gap(evidence)
    if label == "false_positive_timestamp_or_history_rewrite":
        return timestamp_reason(evidence, gap)
    if label == "false_positive_preexisting_dependency":
        return "Fresh first-parent file contains the same candidate spec/version; target commit did not introduce it."
    if label == "confirmed_nonexistent_version_at_commit":
        return f"Target commit introduced exact {form['package_name']}@{form['version']}; official npm publication followed committer time by {gap} seconds."
    if label == "likely_nonexistent_version_at_commit":
        return f"Target commit introduced exact {form['package_name']}@{form['version']}, but npm publication followed committer time by only {gap} seconds."
    return generic_reason(label)


def timestamp_reason(evidence: dict[str, str], gap: int | None) -> str:
    if root_history_rewrite(evidence):
        return "Root commit predates official repository creation; Git history was imported/backdated."
    return f"Official npm publication was at or before GitHub committer time (gap_seconds={gap})."


def generic_reason(label: str) -> str:
    reasons = {
        "false_positive_mapping": "Package/import/registry target mapping is not securely established.",
        "false_positive_private_custom_or_local_registry": "Dependency uses a non-public-registry spec; public npm time is not dispositive.",
        "false_positive_generated_or_vendored_context": "Candidate occurs in generated or vendored context.",
        "false_positive_version_resolution": "Child evidence does not support the candidate exact version.",
        "exclude_not_agent_or_corrupt_source": "Strict agent attribution evidence failed.",
        "unverifiable_missing_historical_evidence": "Required history or official registry evidence remains incomplete.",
    }
    return reasons[label]


def note_for(label: str, evidence: dict[str, str]) -> str:
    parts = [
        f"evidence_grade={evidence_grade(label)}",
        f"publish_after_committer_seconds={publication_gap(evidence)}",
        "sources=fresh GitHub commit/repo/raw + official npm packument",
    ]
    if evidence.get("fresh_parent_count") == "0":
        parts.append(f"repo_created_at={evidence.get('fresh_repo_created_at', '')}")
    return "; ".join(parts)


def decide_case(form: dict[str, str], evidence: dict[str, str]) -> dict[str, str]:
    checks = all_checks(form, evidence)
    label = decide_label(form, evidence, checks)
    return {
        **checks, "label": label,
        "reason": reason_for(label, form, evidence),
        "confidence": confidence(label),
        "notes": note_for(label, evidence),
    }


def source_path(args: argparse.Namespace) -> Path:
    name = "calibration_reviewer_1.csv" if args.phase == "calibration" else "reviewer_1_form.csv"
    return args.review_root / name


def output_path(args: argparse.Namespace) -> Path:
    name = "codex_calibration_locked.csv" if args.phase == "calibration" else "codex_review_form_locked.csv"
    return args.evidence_dir / name


def ensure_phase_ready(args: argparse.Namespace) -> None:
    if args.phase != "full":
        return
    required = ("codex_calibration_locked.csv", "codebook_interpretation_addendum.md")
    missing = [name for name in required if not (args.evidence_dir / name).exists()]
    if missing:
        raise RuntimeError(f"full phase requires: {missing}")


def ensure_blind_source(rows: list[dict[str, str]]) -> None:
    filled = [(row["case_id"], field) for row in rows for field in DECISION_FIELDS if row.get(field, "").strip()]
    if filled:
        raise ValueError(f"source form has prefilled decisions: {filled[:5]}")


def evidence_index(path: Path) -> dict[str, dict[str, str]]:
    return {row["case_id"]: row for row in load_rows(path)}


def completed_rows(forms: list[dict[str, str]], evidence: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    result = []
    for form in forms:
        values = {**form, "reviewer_id": "codex_independent"}
        values.update(decide_case(form, evidence[form["case_id"]]))
        result.append(values)
    return result


def write_review_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def protected_unchanged(evidence_dir: Path, review_root: Path) -> int:
    baseline = json.loads((evidence_dir / "input_baseline.json").read_text(encoding="utf-8"))
    changed = 0
    for name, saved in baseline["files"].items():
        changed += fingerprint(review_root / name)["sha256"] != saved["sha256"]
    return changed


def review_invariants(args: argparse.Namespace, rows: list[dict[str, str]]) -> dict[str, Any]:
    expected = 10 if args.phase == "calibration" else 38
    incomplete = sum(not row.get(field, "").strip() for row in rows for field in DECISION_FIELDS)
    illegal = sum(row["label"] not in LEGAL_LABELS for row in rows)
    changed = protected_unchanged(args.evidence_dir, args.review_root)
    passed = len(rows) == expected and incomplete == 0 and illegal == 0 and changed == 0
    return {
        "status": "pass" if passed else "fail", "phase": args.phase,
        "expected_rows": expected, "rows": len(rows),
        "unique_case_ids": len({row["case_id"] for row in rows}),
        "incomplete_decision_cells": incomplete, "illegal_labels": illegal,
        "protected_input_changes": changed, "adjudicated_labels_written": 0,
    }


def nested_counts(rows: list[dict[str, str]], field: str) -> dict[str, dict[str, int]]:
    result: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        result[row[field]][row["label"]] += 1
    return {key: dict(value) for key, value in sorted(result.items())}


def review_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    commits = {(row["repo"], row["sha"], row["label"]) for row in rows}
    return {
        "cases": len(rows), "unique_commits": len({item[:2] for item in commits}),
        "label_counts": dict(Counter(row["label"] for row in rows)),
        "unique_commit_label_counts": dict(Counter(item[2] for item in commits)),
        "by_language": nested_counts(rows, "language"),
        "by_agent": nested_counts(rows, "agent"),
        "confidence_counts": dict(Counter(row["confidence"] for row in rows)),
    }


def report_text(summary: dict[str, Any]) -> str:
    labels = "\n".join(f"- {key}: {value}" for key, value in summary["label_counts"].items())
    return REPORT_TEMPLATE.format(
        cases=summary["cases"], commits=summary["unique_commits"], labels=labels
    )


def write_phase_outputs(args: argparse.Namespace, rows: list[dict[str, str]], source: Path, target: Path) -> None:
    invariants = review_invariants(args, rows)
    prefix = "codex_calibration" if args.phase == "calibration" else "codex_review"
    invariant_path = args.evidence_dir / f"{prefix}_invariants.json"
    manifest_path = args.evidence_dir / f"{prefix}_manifest.json"
    write_json(invariant_path, invariants)
    summary = review_summary(rows)
    extra = write_full_outputs(args, summary) if args.phase == "full" else []
    manifest = phase_manifest(args, source, target, invariant_path, extra, invariants)
    write_json(manifest_path, manifest)


def write_full_outputs(args: argparse.Namespace, summary: dict[str, Any]) -> list[Path]:
    summary_path = args.evidence_dir / "codex_review_summary.json"
    report_path = args.evidence_dir / "codex_independent_review_report.md"
    write_json(summary_path, summary)
    report_path.write_text(report_text(summary), encoding="utf-8")
    return [summary_path, report_path]


def phase_manifest(args: argparse.Namespace, source: Path, target: Path, invariant: Path, extra: list[Path], values: dict[str, Any]) -> dict[str, Any]:
    inputs = [source, args.evidence_dir / "official_evidence_matrix.csv", args.evidence_dir / "codebook_interpretation_addendum.md"]
    outputs = [target, invariant, *extra]
    return {
        "schema_version": 1, "phase": args.phase,
        "inputs": {path.name: fingerprint(path) for path in inputs},
        "outputs": {path.name: fingerprint(path) for path in outputs},
        "invariants": values,
    }


def main() -> None:
    args = parse_args()
    ensure_phase_ready(args)
    source = source_path(args)
    forms = load_rows(source)
    ensure_blind_source(forms)
    evidence = evidence_index(args.evidence_dir / "official_evidence_matrix.csv")
    rows = completed_rows(forms, evidence)
    target = output_path(args)
    write_review_csv(target, rows)
    write_phase_outputs(args, rows, source, target)
    print(json.dumps(review_summary(rows), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

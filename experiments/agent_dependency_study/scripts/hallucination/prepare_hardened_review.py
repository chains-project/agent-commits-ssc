"""Prepare blinded two-reviewer forms for hardened RQ3 history candidates.

[IN]: Hardened case_comparison.csv/comparison_invariants.json and the
historical review_form.csv plus frozen validation_codebook.md.
[OUT]: Internal coordinator queue, independent reviewer 1/2 forms, separate
10-case calibration forms, public redacted queue, manifests, invariants, and a
queue-readiness report under the hardened product's human_review directory.
[POS]: Offline bridge from pipeline hardening to human adjudication. It never
prefills reviewer decisions, preliminary labels, or adjudication.
[SYNC]: If fields, calibration logic, blinding, or output names change, update
tests, scripts/CLAUDE.md, scripts/OUTPUTS.md, and the active review-queue plan.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from build_validation_cases import fingerprint_file
from prepare_validation_review import (
    MANUAL_FIELDS,
    REVIEW_FIELDS,
    atomic_csv,
    atomic_json,
    after_committer,
    read_csv,
    stable_rank,
)


HARDENED_ROOT = Path("data_products/rq2_hallucinated_strong_label_hardened_20260722")
HISTORICAL_ROOT = Path("data_products/rq2_hallucinated_validation_20260721")
OUTPUT_DIR = HARDENED_ROOT / "human_review"
TARGET_TRANSITION = "future_publish_candidate_only"
TARGET_LABEL = "version_published_after_author_date_candidate"
CALIBRATION_SIZE = 10
CALIBRATION_SEED = "rq3-validation-codebook-v1"
SCHEMA_VERSION = 1

HARDENING_FIELDS = (
    "historical_calibration_selected",
    "old_pipeline_final_label",
    "hardening_transition",
    "hardened_label_reasons",
    "hardened_query_packages",
    "hardened_query_versions",
    "hardened_declared_packages",
    "hardened_resolution_sources",
    "hardened_dependency_groups",
    "hardened_dep_file_paths",
    "hardened_source_files",
)
INTERNAL_FIELDS = ("review_order",) + HARDENING_FIELDS + REVIEW_FIELDS
DECISION_FIELDS = (
    "agent_attribution_check",
    "mapping_check",
    "parent_child_check",
    "timestamp_check",
    "registry_check",
    "private_custom_local_check",
    "generated_vendored_check",
    "label",
    "reason",
    "confidence",
    "notes",
)
BLIND_HIDDEN_FIELDS = frozenset(MANUAL_FIELDS) | {
    "calibration_stratum",
    "automatic_evidence_tier",
    "automatic_review_reason",
    "historical_calibration_selected",
    "old_pipeline_final_label",
}
BLIND_EVIDENCE_FIELDS = tuple(
    field for field in INTERNAL_FIELDS if field not in BLIND_HIDDEN_FIELDS
)
REVIEWER_FIELDS = ("reviewer_id",) + BLIND_EVIDENCE_FIELDS + DECISION_FIELDS
PUBLIC_FIELDS = (
    "review_order",
    "redacted_case_id",
    "calibration_selected",
    "language",
    "agent",
    "pipeline_final_label",
    "parent_count",
    "parent_child_state",
    "local_change_class",
    "dependency_file_changed",
    "publish_after_author_seconds",
    "publish_after_committer_seconds",
    "generated_or_vendored_path_hint",
    "non_registry_spec_hint",
)
PUBLIC_FORBIDDEN = {
    "case_id", "repo", "sha", "package_name", "version",
    "local_evidence_path", "github_evidence_path",
    "agent_author_name", "agent_author_email", "commit_message_first_line",
    "commit_html_url",
}
COMPARISON_MAP = {
    "old_pipeline_final_label": "old_pipeline_label",
    "hardening_transition": "transition_class",
    "hardened_label_reasons": "new_label_reasons",
    "hardened_query_packages": "new_query_packages",
    "hardened_query_versions": "new_query_versions",
    "hardened_declared_packages": "new_declared_packages",
    "hardened_resolution_sources": "new_resolution_sources",
    "hardened_dependency_groups": "new_dependency_groups",
    "hardened_dep_file_paths": "new_dep_file_paths",
    "hardened_source_files": "new_source_files",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardened-root", type=Path, default=HARDENED_ROOT)
    parser.add_argument("--historical-root", type=Path, default=HISTORICAL_ROOT)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--calibration-size", type=int, default=CALIBRATION_SIZE)
    parser.add_argument("--seed", default=CALIBRATION_SEED)
    return parser.parse_args()


def comparison_fields(row: dict[str, str]) -> dict[str, str]:
    return {
        target: row.get(source, "")
        for target, source in COMPARISON_MAP.items()
    }


def evidence_path(root: Path | None, value: str) -> str:
    if not value or root is None or Path(value).is_absolute():
        return value
    return str(root / value)


def build_hardened_rows(
    comparison: list[dict[str, str]],
    historical_review: list[dict[str, str]],
    historical_root: Path | None = None,
) -> list[dict[str, str]]:
    review_index = {row["case_id"]: row for row in historical_review}
    candidates = [
        row for row in comparison
        if row.get("transition_class") == TARGET_TRANSITION
    ]
    missing = [row["case_id"] for row in candidates if row["case_id"] not in review_index]
    if missing:
        raise ValueError(f"historical review rows missing for {len(missing)} cases")
    rows = [
        build_hardened_row(row, review_index[row["case_id"]], historical_root)
        for row in candidates
    ]
    return sorted(rows, key=lambda row: row["case_id"])


def build_hardened_row(
    comparison: dict[str, str],
    historical: dict[str, str],
    historical_root: Path | None,
) -> dict[str, str]:
    row = {field: "" for field in INTERNAL_FIELDS}
    row.update({field: historical.get(field, "") for field in REVIEW_FIELDS})
    row.update(comparison_fields(comparison))
    row["historical_calibration_selected"] = historical.get(
        "calibration_selected", "false"
    )
    row["calibration_selected"] = "false"
    row["pipeline_final_label"] = comparison.get("new_final_labels", "")
    row["local_evidence_path"] = evidence_path(
        historical_root, row["local_evidence_path"]
    )
    row["github_evidence_path"] = evidence_path(
        historical_root, row["github_evidence_path"]
    )
    for field in MANUAL_FIELDS:
        row[field] = ""
    return row


def assign_calibration_and_order(
    rows: list[dict[str, str]], size: int, seed: str,
) -> set[str]:
    selected = {
        row["case_id"] for row in select_balanced_calibration(rows, size, seed)
    }
    ordered = sorted(
        rows,
        key=lambda row: (
            row["case_id"] not in selected,
            stable_rank(seed + "|review-order", row["case_id"]),
        ),
    )
    for index, row in enumerate(ordered, 1):
        row["review_order"] = str(index)
        row["calibration_selected"] = str(row["case_id"] in selected).lower()
    rows[:] = ordered
    return selected


def calibration_features(row: dict[str, str]) -> set[str]:
    return {
        f"language:{row.get('language', '')}",
        f"agent:{row.get('agent', '')}",
        f"parent:{row.get('parent_child_state', '')}",
        f"after_committer:{after_committer(row)}",
        f"tier:{row.get('automatic_evidence_tier', '')}",
    }


def select_balanced_calibration(
    rows: list[dict[str, str]], size: int, seed: str,
) -> list[dict[str, str]]:
    if size < 1 or size > len(rows):
        raise ValueError("calibration size must be within available rows")
    frequencies = Counter(
        feature for row in rows for feature in calibration_features(row)
    )
    uncovered = set(frequencies)
    high_total = sum(
        row.get("automatic_evidence_tier") == "priority_high" for row in rows
    )
    high_limit = 1 if high_total >= 2 else high_total
    selected: list[dict[str, str]] = []
    while len(selected) < size:
        eligible = eligible_calibration_rows(
            rows, selected, high_limit
        )
        if not eligible:
            raise ValueError("calibration constraints leave too few rows")
        chosen = min(
            eligible,
            key=lambda row: calibration_rank(
                row, uncovered, frequencies, seed
            ),
        )
        selected.append(chosen)
        uncovered -= calibration_features(chosen)
    return sorted(selected, key=lambda row: row["case_id"])


def eligible_calibration_rows(
    rows: list[dict[str, str]],
    selected: list[dict[str, str]],
    high_limit: int,
) -> list[dict[str, str]]:
    selected_ids = {row["case_id"] for row in selected}
    selected_high = sum(
        row.get("automatic_evidence_tier") == "priority_high"
        for row in selected
    )
    return [
        row for row in rows
        if row["case_id"] not in selected_ids
        and (
            row.get("automatic_evidence_tier") != "priority_high"
            or selected_high < high_limit
        )
    ]


def calibration_rank(
    row: dict[str, str],
    uncovered: set[str],
    frequencies: Counter[str],
    seed: str,
) -> tuple[float, float, str]:
    new_features = calibration_features(row) & uncovered
    rarity = sum(1 / frequencies[feature] for feature in new_features)
    return -len(new_features), -rarity, stable_rank(seed, row["case_id"])


def blind_reviewer_row(
    row: dict[str, str], reviewer_id: str,
) -> dict[str, str]:
    blinded = {
        field: row.get(field, "") for field in BLIND_EVIDENCE_FIELDS
    }
    blinded["reviewer_id"] = reviewer_id
    blinded.update({field: "" for field in DECISION_FIELDS})
    return {field: blinded.get(field, "") for field in REVIEWER_FIELDS}


def public_row(row: dict[str, str]) -> dict[str, str]:
    return {field: row.get(field, "") for field in PUBLIC_FIELDS}


def count_filled(
    rows: list[dict[str, str]], fields: tuple[str, ...],
) -> int:
    return sum(bool(row.get(field, "")) for row in rows for field in fields)


def reviewer_evidence(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(row.get(field, "") for field in BLIND_EVIDENCE_FIELDS)


def validate_queue(
    internal: list[dict[str, str]],
    reviewer_1: list[dict[str, str]],
    reviewer_2: list[dict[str, str]],
    expected_cases: int,
    calibration_size: int,
) -> dict[str, Any]:
    context = queue_context(internal, reviewer_1, reviewer_2)
    report = queue_metrics(
        internal, reviewer_1, reviewer_2, expected_cases, calibration_size,
        context,
    )
    return {
        "status": "fail" if queue_has_failures(
            report, expected_cases, calibration_size
        ) else "pass",
        **report,
    }


def queue_context(
    internal: list[dict[str, str]],
    reviewer_1: list[dict[str, str]],
    reviewer_2: list[dict[str, str]],
) -> dict[str, Any]:
    ids = [row.get("case_id", "") for row in internal]
    selected = [row for row in internal if row.get("calibration_selected") == "true"]
    return {
        "ids": ids,
        **calibration_context(internal, selected),
        **reviewer_pair_context(ids, reviewer_1, reviewer_2),
    }


def calibration_context(
    internal: list[dict[str, str]],
    selected: list[dict[str, str]],
) -> dict[str, Any]:
    features = set().union(*(calibration_features(row) for row in internal))
    selected_features = set().union(*(
        calibration_features(row) for row in selected
    ))
    high_total = sum(
        row.get("automatic_evidence_tier") == "priority_high"
        for row in internal
    )
    high_calibration = sum(
        row.get("automatic_evidence_tier") == "priority_high"
        for row in selected
    )
    return {
        "strata": {row.get("calibration_stratum", "") for row in internal},
        "selected": selected,
        "selected_strata": {
            row.get("calibration_stratum", "") for row in selected
        },
        "features": features,
        "selected_features": selected_features,
        "priority_high_total": high_total,
        "priority_high_calibration": high_calibration,
    }


def reviewer_pair_context(
    ids: list[str],
    reviewer_1: list[dict[str, str]],
    reviewer_2: list[dict[str, str]],
) -> dict[str, Any]:
    reviewer_ids = lambda rows: [
        row.get("case_id", "") for row in rows
    ]
    return {
        "reviewer_ids_match": (
            reviewer_ids(reviewer_1) == reviewer_ids(reviewer_2) == ids
        ),
        "evidence_mismatch": sum(
            reviewer_evidence(left) != reviewer_evidence(right)
            for left, right in zip(reviewer_1, reviewer_2)
        ),
    }


def queue_has_failures(
    report: dict[str, Any], expected_cases: int, calibration_size: int,
) -> bool:
    return (
        report["review_rows"] != expected_cases
        or report["unique_case_ids"] != expected_cases
        or report["calibration_rows"] != calibration_size
        or any(report[key] for key in queue_failure_fields())
    )


def queue_metrics(
    internal: list[dict[str, str]],
    reviewer_1: list[dict[str, str]],
    reviewer_2: list[dict[str, str]],
    expected_cases: int,
    calibration_size: int,
    context: dict[str, Any],
) -> dict[str, Any]:
    return {
        **case_queue_metrics(internal, expected_cases, context["ids"]),
        **blind_queue_metrics(
            reviewer_1, reviewer_2, expected_cases, context
        ),
        **calibration_queue_metrics(
            calibration_size, context
        ),
    }


def case_queue_metrics(
    internal: list[dict[str, str]], expected_cases: int, ids: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "expected_cases": expected_cases,
        "review_rows": len(internal),
        "unique_case_ids": len(set(ids)),
        "missing_case_ids": sum(not value for value in ids),
        "candidate_label_mismatches": sum(
            row.get("pipeline_final_label") != TARGET_LABEL for row in internal
        ),
        "prefilled_internal_manual_cells": count_filled(
            internal, MANUAL_FIELDS
        ),
    }


def blind_queue_metrics(
    reviewer_1: list[dict[str, str]],
    reviewer_2: list[dict[str, str]],
    expected_cases: int,
    context: dict[str, Any],
) -> dict[str, Any]:
    return {
        "prefilled_blind_decision_cells": count_filled(
            reviewer_1 + reviewer_2, DECISION_FIELDS
        ),
        "reviewer_row_count_mismatches": (
            int(len(reviewer_1) != expected_cases)
            + int(len(reviewer_2) != expected_cases)
        ),
        "reviewer_case_order_mismatch": int(
            not context["reviewer_ids_match"]
        ),
        "reviewer_evidence_mismatches": context["evidence_mismatch"],
    }


def calibration_queue_metrics(
    calibration_size: int, context: dict[str, Any],
) -> dict[str, Any]:
    selected = context["selected"]
    high_holdout = (
        context["priority_high_total"] - context["priority_high_calibration"]
    )
    return {
        "calibration_rows": len(selected),
        "requested_calibration_rows": calibration_size,
        "observed_strata": len(context["strata"]),
        "covered_strata": len(context["selected_strata"]),
        "observed_marginal_features": len(context["features"]),
        "covered_marginal_features": len(context["selected_features"]),
        "calibration_uncovered_features": len(
            context["features"] - context["selected_features"]
        ),
        "priority_high_total": context["priority_high_total"],
        "priority_high_calibration": context["priority_high_calibration"],
        "priority_high_holdout": high_holdout,
        "priority_high_holdout_failures": int(
            context["priority_high_total"] >= 2 and high_holdout < 1
        ),
        "historical_calibration_retained": sum(
            row.get("historical_calibration_selected") == "true"
            for row in selected
        ),
    }


def queue_failure_fields() -> tuple[str, ...]:
    return (
        "missing_case_ids",
        "candidate_label_mismatches",
        "prefilled_internal_manual_cells",
        "prefilled_blind_decision_cells",
        "reviewer_row_count_mismatches",
        "reviewer_case_order_mismatch",
        "reviewer_evidence_mismatches",
        "calibration_uncovered_features",
        "priority_high_holdout_failures",
    )


def input_paths(args: argparse.Namespace) -> dict[str, Path]:
    return {
        "comparison": args.hardened_root / "case_comparison.csv",
        "comparison_invariants": args.hardened_root / "comparison_invariants.json",
        "historical_review": args.historical_root / "review_form.csv",
        "codebook": args.historical_root / "validation_codebook.md",
    }


def selection_metadata(
    rows: list[dict[str, str]],
    selected: set[str],
    args: argparse.Namespace,
    paths: dict[str, Path],
) -> dict[str, Any]:
    strata = Counter(row["calibration_stratum"] for row in rows)
    selected_strata = Counter(
        row["calibration_stratum"] for row in rows
        if row["case_id"] in selected
    )
    features, selected_features = calibration_feature_counters(
        rows, selected
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "calibration_size": args.calibration_size,
        "selected_case_ids": sorted(selected),
        "strata_counts": dict(sorted(strata.items())),
        "selected_strata_counts": dict(sorted(selected_strata.items())),
        "marginal_feature_counts": dict(sorted(features.items())),
        "selected_marginal_feature_counts": dict(
            sorted(selected_features.items())
        ),
        "input_fingerprints": {
            name: fingerprint_file(path) for name, path in paths.items()
        },
    }


def calibration_feature_counters(
    rows: list[dict[str, str]], selected: set[str],
) -> tuple[Counter[str], Counter[str]]:
    features = Counter(
        feature for row in rows for feature in calibration_features(row)
    )
    selected_features = Counter(
        feature for row in rows if row["case_id"] in selected
        for feature in calibration_features(row)
    )
    return features, selected_features


def queue_report(
    rows: list[dict[str, str]], invariants: dict[str, Any],
    metadata: dict[str, Any],
) -> str:
    languages = Counter(row["language"] for row in rows)
    agents = Counter(row["agent"] for row in rows)
    lines = queue_report_summary(
        rows, invariants, languages, agents
    ) + queue_report_guidance(metadata)
    return "\n".join(lines)


def queue_report_summary(
    rows: list[dict[str, str]],
    invariants: dict[str, Any],
    languages: Counter[str],
    agents: Counter[str],
) -> list[str]:
    return [
        "# Hardened candidate human-review queue readiness", "",
        f"- Candidate cases: {len(rows)}",
        f"- Unique commits: {len({(row['repo'], row['sha']) for row in rows})}",
        f"- Calibration cases: {invariants['calibration_rows']}",
        f"- Marginal features observed/covered: {invariants['observed_marginal_features']}/{invariants['covered_marginal_features']}",
        f"- Exact strata observed/covered (descriptive): {invariants['observed_strata']}/{invariants['covered_strata']}",
        f"- Priority-high calibration/holdout: {invariants['priority_high_calibration']}/{invariants['priority_high_holdout']}",
        f"- Historical calibration cases retained: {invariants['historical_calibration_retained']}",
        f"- Invariant status: {invariants['status']}", "",
        "## Composition", "",
        f"- Languages: {dict(sorted(languages.items()))}",
        f"- Agents: {dict(sorted(agents.items()))}", "",
    ]


def queue_report_guidance(metadata: dict[str, Any]) -> list[str]:
    return [
        "## Blinding", "",
        "- Reviewer forms exclude previous preliminary labels, automatic priority tiers/reasons, calibration strata, the other reviewer's fields, and adjudication.",
        "- Reviewer 1 and reviewer 2 receive the same evidence in the same deterministic order, with independent empty decision fields.",
        "- Public output excludes package, version, repository, SHA, evidence paths, author identity, commit message, and URL.", "",
        "## Workflow", "",
        "1. Each reviewer independently completes only their 10-case calibration form.",
        "2. The coordinator compares calibration disagreements and clarifies the codebook without creating case-specific answer rules.",
        "3. Under the frozen codebook, each reviewer independently completes their untouched 38-case full form.",
        "4. Preserve both raw forms, compute agreement on the 28 non-calibration holdout cases, and adjudicate disagreements separately.", "",
        f"Selection seed: `{metadata['seed']}`", "",
    ]


def review_protocol() -> str:
    lines = [
        "# Hardened RQ3 human-review protocol", "",
        "This queue contains 38 history-validation candidates. The pipeline label is not a human conclusion and does not prove AI causality.", "",
        "## Reviewer workflow", "",
        "1. Do not open the coordinator form, the other reviewer form, or historical preliminary decisions.",
        "2. Complete only your 10-case calibration file first.",
        "3. After codebook clarification, start from your untouched 38-case full form and independently review every row.",
        "4. Do not copy decisions from the calibration file into the full form without re-evaluating the evidence under the frozen codebook.",
        "5. Preserve the submitted CSV unchanged after handoff; adjudication happens in a separate artifact.", "",
        "## Decision fields", "",
        "- Seven `*_check` fields: `pass`, `fail`, `uncertain`, or `not_applicable`.",
        "- `label`: one exact label from the attached frozen codebook.",
        "- `reason`: concise evidence-based justification; required.",
        "- `confidence`: `high`, `medium`, or `low`.",
        "- `notes`: optional ambiguity or follow-up evidence request.", "",
        "Evidence paths are internal project-relative paths. The public redacted queue is not sufficient for adjudication.", "",
    ]
    return "\n".join(lines)


def codebook_snapshot(metadata: dict[str, Any]) -> str:
    source = Path(metadata["input_fingerprints"]["codebook"]["path"])
    return source.read_text(encoding="utf-8-sig")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def prepare(args: argparse.Namespace) -> tuple[
    list[dict[str, str]], list[dict[str, str]],
    list[dict[str, str]], dict[str, Any], dict[str, Path],
]:
    paths = input_paths(args)
    rows = build_hardened_rows(
        read_csv(paths["comparison"]),
        read_csv(paths["historical_review"]),
        args.historical_root,
    )
    selected = assign_calibration_and_order(
        rows, args.calibration_size, args.seed
    )
    reviewer_1 = [blind_reviewer_row(row, "reviewer_1") for row in rows]
    reviewer_2 = [blind_reviewer_row(row, "reviewer_2") for row in rows]
    metadata = selection_metadata(rows, selected, args, paths)
    return rows, reviewer_1, reviewer_2, metadata, paths


def write_outputs(
    args: argparse.Namespace,
    rows: list[dict[str, str]],
    reviewer_1: list[dict[str, str]],
    reviewer_2: list[dict[str, str]],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    calibration_internal, calibration_1, calibration_2 = calibration_subsets(
        rows, reviewer_1, reviewer_2, set(metadata["selected_case_ids"])
    )
    public = [public_row(row) for row in rows]
    invariants = validate_queue(
        rows, reviewer_1, reviewer_2, len(rows), args.calibration_size
    )
    apply_public_invariants(invariants)
    outputs = output_rows(
        rows, reviewer_1, reviewer_2, calibration_internal,
        calibration_1, calibration_2, public,
    )
    write_queue_artifacts(args, outputs, metadata, invariants, rows)
    return invariants


def calibration_subsets(
    rows: list[dict[str, str]],
    reviewer_1: list[dict[str, str]],
    reviewer_2: list[dict[str, str]],
    calibration_ids: set[str],
) -> tuple[
    list[dict[str, str]], list[dict[str, str]], list[dict[str, str]],
]:
    keep = lambda items: [
        row for row in items if row["case_id"] in calibration_ids
    ]
    return keep(rows), keep(reviewer_1), keep(reviewer_2)


def apply_public_invariants(invariants: dict[str, Any]) -> None:
    invariants["public_forbidden_columns"] = len(
        set(PUBLIC_FIELDS) & PUBLIC_FORBIDDEN
    )
    if invariants["public_forbidden_columns"]:
        invariants["status"] = "fail"


def write_queue_artifacts(
    args: argparse.Namespace,
    outputs: dict[str, tuple[list[dict[str, str]], tuple[str, ...]]],
    metadata: dict[str, Any],
    invariants: dict[str, Any],
    rows: list[dict[str, str]],
) -> None:
    for name, (items, fields) in outputs.items():
        atomic_csv(args.output_dir / name, items, fields)
    atomic_json(args.output_dir / "review_selection_manifest.json", metadata)
    atomic_json(args.output_dir / "review_queue_invariants.json", invariants)
    write_text(
        args.output_dir / "queue_readiness_report.md",
        queue_report(rows, invariants, metadata),
    )
    write_text(
        args.output_dir / "review_protocol.md", review_protocol()
    )
    write_text(
        args.output_dir / "validation_codebook_v1_snapshot.md",
        codebook_snapshot(metadata),
    )
    write_output_manifest(args.output_dir, outputs)


def output_rows(
    rows: list[dict[str, str]],
    reviewer_1: list[dict[str, str]],
    reviewer_2: list[dict[str, str]],
    calibration_internal: list[dict[str, str]],
    calibration_1: list[dict[str, str]],
    calibration_2: list[dict[str, str]],
    public: list[dict[str, str]],
) -> dict[str, tuple[list[dict[str, str]], tuple[str, ...]]]:
    return {
        "hardened_review_form.csv": (rows, INTERNAL_FIELDS),
        "reviewer_1_form.csv": (reviewer_1, REVIEWER_FIELDS),
        "reviewer_2_form.csv": (reviewer_2, REVIEWER_FIELDS),
        "calibration_cases.csv": (calibration_internal, INTERNAL_FIELDS),
        "calibration_reviewer_1.csv": (calibration_1, REVIEWER_FIELDS),
        "calibration_reviewer_2.csv": (calibration_2, REVIEWER_FIELDS),
        "public_redacted_hardened_queue.csv": (public, PUBLIC_FIELDS),
    }


def write_output_manifest(
    output_dir: Path,
    outputs: dict[str, tuple[list[dict[str, str]], tuple[str, ...]]],
) -> None:
    names = list(outputs) + [
        "review_selection_manifest.json",
        "review_queue_invariants.json",
        "queue_readiness_report.md",
        "review_protocol.md",
        "validation_codebook_v1_snapshot.md",
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "outputs": {
            name: fingerprint_file(output_dir / name) for name in names
        },
    }
    atomic_json(output_dir / "review_queue_manifest.json", manifest)


def main() -> None:
    args = parse_args()
    rows, reviewer_1, reviewer_2, metadata, _paths = prepare(args)
    invariants = write_outputs(
        args, rows, reviewer_1, reviewer_2, metadata
    )
    print(json.dumps({
        "review_rows": len(rows),
        "calibration_rows": args.calibration_size,
        "invariant_status": invariants["status"],
        "output_dir": str(args.output_dir),
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

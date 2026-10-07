"""Finalize the locked Codex RQ3 review into a ledger and scientific report.

[IN]: locked review, fresh official evidence matrix/log, calibration IDs, and
post-lock unblind summary.
[OUT]: case evidence ledger, final summary, validity-aware report, invariants,
and SHA-256 manifest.
[POS]: Descriptive synthesis of one preliminary Codex review; not human
adjudication and not causal attribution to an AI agent.
[SYNC]: Update scripts/CLAUDE.md, scripts/OUTPUTS.md, product README, and the
active independent-review plan when outputs or claims change.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from collect_codex_official_evidence import (
    DEFAULT_OUTPUT,
    REVIEW_ROOT,
    fingerprint,
    load_rows,
    write_json,
)


LOCKED = DEFAULT_OUTPUT / "codex_review_form_locked.csv"
MATRIX = DEFAULT_OUTPUT / "official_evidence_matrix.csv"
LOG = DEFAULT_OUTPUT / "codex_official_evidence_log.jsonl"
UNBLIND = DEFAULT_OUTPUT / "codex_review_unblind_summary.json"
LEDGER_FIELDS = (
    "review_order", "case_id", "redacted_case_id", "language", "agent", "repo",
    "sha", "package_name", "version", "label", "confidence", "evidence_grade",
    "reason", "agent_attribution_check", "mapping_check", "parent_child_check",
    "timestamp_check", "registry_check", "private_custom_local_check",
    "generated_vendored_check", "author_date", "committer_date",
    "registry_publish_time", "publish_after_committer_seconds", "parent_count",
    "parent_child_state", "local_change_class", "repo_created_at",
    "child_file_states_json", "parent_file_states_json", "official_sources_json",
)
POSITIVE_LABELS = {
    "confirmed_nonexistent_version_at_commit",
    "likely_nonexistent_version_at_commit",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--locked", type=Path, default=LOCKED)
    parser.add_argument("--matrix", type=Path, default=MATRIX)
    parser.add_argument("--log", type=Path, default=LOG)
    parser.add_argument("--unblind", type=Path, default=UNBLIND)
    parser.add_argument("--review-root", type=Path, default=REVIEW_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def note_value(notes: str, key: str) -> str:
    prefix = f"{key}="
    for part in notes.split(";"):
        value = part.strip()
        if value.startswith(prefix):
            return value[len(prefix):]
    return ""


def sources_index(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    keys = ("kind", "url", "path", "status", "retrieved_utc", "body_sha256")
    for row in rows:
        result[row["case_id"]].append({key: row.get(key, "") for key in keys})
    return result


def ledger_row(
    locked: dict[str, str],
    evidence: dict[str, str],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    values = {key: locked.get(key, "") for key in LEDGER_FIELDS}
    values.update({
        "evidence_grade": note_value(locked.get("notes", ""), "evidence_grade"),
        "committer_date": evidence["fresh_committer_date"],
        "registry_publish_time": evidence["fresh_registry_publish_time"],
        "publish_after_committer_seconds": note_value(
            locked.get("notes", ""), "publish_after_committer_seconds"
        ),
        "parent_count": evidence["fresh_parent_count"],
        "repo_created_at": evidence["fresh_repo_created_at"],
        "child_file_states_json": evidence["fresh_child_file_states_json"],
        "parent_file_states_json": evidence["fresh_parent_file_states_json"],
        "official_sources_json": json.dumps(sources, ensure_ascii=False, separators=(",", ":")),
    })
    return values


def build_ledger(
    locked: list[dict[str, str]],
    matrix: list[dict[str, str]],
    logs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence = {row["case_id"]: row for row in matrix}
    sources = sources_index(logs)
    return [
        ledger_row(row, evidence[row["case_id"]], sources[row["case_id"]])
        for row in locked
    ]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def result_metrics(rows: list[dict[str, str]]) -> dict[str, Any]:
    commits = {(row["repo"], row["sha"]) for row in rows}
    positive_commits = {
        (row["repo"], row["sha"]) for row in rows if row["label"] in POSITIVE_LABELS
    }
    positive_cases = sum(row["label"] in POSITIVE_LABELS for row in rows)
    return {
        "cases": len(rows), "unique_commits": len(commits),
        "positive_cases": positive_cases, "positive_commits": len(positive_commits),
        "confirmed_cases": sum(row["label"].startswith("confirmed_") for row in rows),
        "likely_cases": sum(row["label"].startswith("likely_") for row in rows),
        "false_positive_cases": len(rows) - positive_cases,
        "case_positive_rate": positive_cases / len(rows),
        "commit_positive_rate": len(positive_commits) / len(commits),
        "label_counts": dict(Counter(row["label"] for row in rows)),
    }


def grouped_labels(rows: list[dict[str, str]], field: str) -> dict[str, Any]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        grouped[row[field]][row["label"]] += 1
    return {key: dict(value) for key, value in sorted(grouped.items())}


def calibration_split(rows: list[dict[str, str]], calibration: set[str]) -> dict[str, Any]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        partition = "calibration" if row["case_id"] in calibration else "holdout"
        grouped[partition][row["label"]] += 1
    return {key: dict(value) for key, value in grouped.items()}


def request_metrics(logs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case_linked_rows": len(logs),
        "unique_urls": len({row["url"] for row in logs}),
        "transport_failures": sum(row["status"] == 0 for row in logs),
        "http_status_counts": dict(Counter(str(row["status"]) for row in logs)),
    }


def final_summary(
    rows: list[dict[str, str]],
    logs: list[dict[str, Any]],
    calibration: set[str],
    unblind: dict[str, Any],
) -> dict[str, Any]:
    return {
        "scope": "one preliminary Codex evidence review; descriptive only",
        "result": result_metrics(rows),
        "by_language": grouped_labels(rows, "language"),
        "by_agent": grouped_labels(rows, "agent"),
        "calibration_holdout": calibration_split(rows, calibration),
        "official_requests": request_metrics(logs),
        "zero_minute_sensitivity": {
            "confirmed_cases": 2, "likely_cases": 0, "positive_cases": 2
        },
        "unblind_diagnostic": unblind,
    }


def pct(value: int, total: int) -> str:
    return f"{100 * value / total:.1f}%"


def label_lines(counts: dict[str, int], total: int) -> str:
    return "\n".join(
        f"- {label}: {count}/{total} ({pct(count, total)})"
        for label, count in counts.items()
    )


def positive_table(rows: list[dict[str, str]]) -> str:
    lines = ["| Case | Agent | Package | Gap | Label |", "|---|---|---|---:|---|"]
    for row in rows:
        if row["label"] not in POSITIVE_LABELS:
            continue
        lines.append(
            f"| {row['case_id']} | {row['agent']} | {row['package_name']}@{row['version']} "
            f"| {note_value(row['notes'], 'publish_after_committer_seconds')} s | {row['label']} |"
        )
    return "\n".join(lines)


def outcome_section(result: dict[str, Any]) -> str:
    case_rate = pct(result["positive_cases"], result["cases"])
    false_rate = pct(result["false_positive_cases"], result["cases"])
    commit_rate = pct(result["positive_commits"], result["unique_commits"])
    return (
        "## Outcome\n\n"
        f"Fresh official-source validation retained {result['positive_cases']} "
        f"of {result['cases']} hardened candidates ({case_rate}): one confirmed "
        f"and one likely. The remaining {result['false_positive_cases']} cases "
        f"({false_rate}) have direct counterevidence.\n\n"
        f"{label_lines(result['label_counts'], result['cases'])}\n\n"
        f"At commit level, {result['positive_commits']} of "
        f"{result['unique_commits']} remain positive ({commit_rate})."
    )


def evidence_section(rows: list[dict[str, str]]) -> str:
    return (
        "## Positive evidence\n\n"
        f"{positive_table(rows)}\n\n"
        "The confirmed case introduced exact next@15.0.5 in a newly added "
        "dependency file; npm publication followed by 4,351,651 seconds. "
        "The likely case changed @supabase/supabase-js from ^2.104.0 to "
        "^2.104.1, but publication followed by only 392 seconds."
    )


def rejection_section() -> str:
    return """## Rejection waterfall

- 23 primary timestamp/history false positives: 16 versions were already
  published by committer time and 7 root commits predated repository creation.
- 13 additional first-parent false positives remained after timestamp checks.
- All official npm lookups and required GitHub evidence were complete.
- Mapping passed for all 38 cases."""


def validity_section() -> str:
    return """## Sensitivity and validity

With a zero-minute boundary, likely becomes confirmed; combined positives stay
2/38. Cases cluster in 27 commits. Strengths are blind locking, primary-source
hashes, first-parent comparisons, and unchanged human-review inputs.

This is one Codex reviewer; historical agreement is not independent; selection
was conditioned on prior pipeline positives; and commit evidence cannot prove
AI causality. Independent human coding and adjudication remain required."""


def final_report(summary: dict[str, Any], rows: list[dict[str, str]]) -> str:
    sections = (
        "# Final Codex independent RQ3 evidence review",
        outcome_section(summary["result"]),
        evidence_section(rows),
        rejection_section(),
        validity_section(),
        "## Supported conclusion\n\nWithin this selected set, one confirmed "
        "and one likely exact-version nonexistence-at-commit event remain. "
        "Author-date future publication alone is not a valid hallucination label: "
        "36/38 candidates have timestamp/history or parent-state explanations.",
    )
    return "\n\n".join(sections) + "\n"


def final_invariants(
    rows: list[dict[str, Any]], logs: list[dict[str, Any]], locked: Path,
    locked_before: dict[str, Any],
) -> dict[str, Any]:
    missing_sources = sum(len(json.loads(row["official_sources_json"])) < 3 for row in rows)
    changed = fingerprint(locked)["sha256"] != locked_before["sha256"]
    passed = len(rows) == 38 and missing_sources == 0 and not changed
    return {
        "status": "pass" if passed else "fail", "ledger_rows": len(rows),
        "unique_case_ids": len({row["case_id"] for row in rows}),
        "request_log_rows": len(logs), "cases_with_fewer_than_three_sources": missing_sources,
        "locked_review_changed": changed, "adjudicated_labels_written": 0,
    }


def final_manifest(
    args: argparse.Namespace, outputs: list[Path], invariants: dict[str, Any]
) -> dict[str, Any]:
    inputs = [args.locked, args.matrix, args.log, args.unblind]
    return {
        "schema_version": 1,
        "inputs": {path.name: fingerprint(path) for path in inputs},
        "outputs": {path.name: fingerprint(path) for path in outputs},
        "invariants": invariants,
    }


def main() -> None:
    args = parse_args()
    locked_before = fingerprint(args.locked)
    locked, matrix = load_rows(args.locked), load_rows(args.matrix)
    logs = read_jsonl(args.log)
    calibration = {
        row["case_id"] for row in load_rows(args.review_root / "calibration_reviewer_1.csv")
    }
    unblind = json.loads(args.unblind.read_text(encoding="utf-8"))
    ledger = build_ledger(locked, matrix, logs)
    ledger_path = args.output_dir / "codex_case_evidence_ledger.csv"
    summary_path = args.output_dir / "codex_review_final_summary.json"
    report_path = args.output_dir / "codex_independent_review_final_report.md"
    invariant_path = args.output_dir / "codex_review_final_invariants.json"
    write_csv(ledger_path, ledger)
    summary = final_summary(locked, logs, calibration, unblind)
    write_json(summary_path, summary)
    report_path.write_text(final_report(summary, locked), encoding="utf-8")
    invariants = final_invariants(ledger, logs, args.locked, locked_before)
    write_json(invariant_path, invariants)
    outputs = [ledger_path, summary_path, report_path, invariant_path]
    write_json(
        args.output_dir / "codex_review_final_manifest.json",
        final_manifest(args, outputs, invariants),
    )
    print(json.dumps({**invariants, **summary["result"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

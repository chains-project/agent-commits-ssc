"""Apply the frozen codebook to a complete preliminary evidence review.

[IN]: review_form.csv, preliminary_review_overrides.json, and
validation_codebook.md.
[OUT]: preliminary_codex_review.csv, public_redacted_preliminary_review.csv,
preliminary_review_summary.json, preliminary_review_report.md, and
preliminary_review_invariants.json.
[POS]: Offline Phase 3/4 evidence synthesis. Results are explicitly
preliminary and do not populate the human adjudication fields.
[SYNC]: If decision precedence, labels, denominators, or public fields change,
update tests, scripts/OUTPUTS.md, the validation README, and the active plan.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from build_validation_cases import fingerprint_file
from prepare_validation_review import atomic_csv, atomic_json, read_csv


DEFAULT_ROOT = Path("data_products/rq2_hallucinated_validation_20260721")
SCHEMA_VERSION = 1
REVIEWER = "codex_preliminary_evidence_review"
CODEBOOK_VERSION = "v1"

DECISION_FIELDS = (
    "case_id", "redacted_case_id", "language", "agent", "repo", "sha",
    "ecosystem", "package_name", "version", "parent_child_state",
    "local_change_class", "publish_after_author_seconds",
    "publish_after_committer_seconds", "automatic_evidence_tier",
    "preliminary_label", "reason_code", "evidence_grade", "confidence",
    "decision_source", "reviewer", "codebook_version",
    "requires_human_confirmation", "local_evidence_path",
    "github_evidence_path",
)

PUBLIC_FIELDS = (
    "redacted_case_id", "language", "agent", "parent_child_state",
    "local_change_class", "automatic_evidence_tier", "preliminary_label",
    "reason_code", "evidence_grade", "confidence", "decision_source",
    "codebook_version", "requires_human_confirmation",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    return parser.parse_args()


def decision_for(
    row: dict[str, str], overrides: dict[str, dict[str, str]]
) -> dict[str, str]:
    case_id = row["case_id"]
    if case_id in overrides:
        item = overrides[case_id]
        return _decision(item["label"], item["reason"], item["grade"], item["confidence"], "case_specific_review")
    tier = row.get("automatic_evidence_tier", "")
    if tier == "contradicted_time":
        return _decision("false_positive_timestamp_or_history_rewrite", "version_not_after_committer", "D", "high", "frozen_rule")
    if tier == "contradicted_parent_introduction":
        return _decision("false_positive_preexisting_dependency", "same_state_in_first_parent", "D", "high", "frozen_rule")
    raise ValueError(f"priority case lacks case-specific review: {case_id}")


def _decision(label: str, reason: str, grade: str, confidence: str, source: str) -> dict[str, str]:
    return {
        "preliminary_label": label, "reason_code": reason,
        "evidence_grade": grade, "confidence": confidence,
        "decision_source": source,
    }


def build_decision(
    row: dict[str, str], overrides: dict[str, dict[str, str]]
) -> dict[str, str]:
    result = {field: row.get(field, "") for field in DECISION_FIELDS}
    result.update(decision_for(row, overrides))
    result["reviewer"] = REVIEWER
    result["codebook_version"] = CODEBOOK_VERSION
    result["requires_human_confirmation"] = "true"
    return result


def public_decision(row: dict[str, str]) -> dict[str, str]:
    return {field: row.get(field, "") for field in PUBLIC_FIELDS}


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def validate_decisions(
    rows: list[dict[str, str]], expected: int
) -> dict[str, Any]:
    ids = [row.get("case_id", "") for row in rows]
    report = {
        "schema_version": SCHEMA_VERSION, "expected_cases": expected,
        "decision_rows": len(rows), "unique_case_ids": len(set(ids)),
        "missing_case_ids": sum(not value for value in ids),
        "missing_labels": sum(not row.get("preliminary_label") for row in rows),
        "human_confirmation_required": sum(row.get("requires_human_confirmation") == "true" for row in rows),
    }
    report["status"] = "pass" if (
        len(rows) == expected == len(set(ids))
        and not report["missing_case_ids"] and not report["missing_labels"]
        and report["human_confirmation_required"] == expected
    ) else "fail"
    return report


def commit_outcomes(rows: list[dict[str, str]]) -> Counter[str]:
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        grouped[(row["repo"], row["sha"])].append(row["preliminary_label"])
    outcomes: Counter[str] = Counter()
    for labels in grouped.values():
        if "confirmed_nonexistent_version_at_commit" in labels:
            outcomes["confirmed_commit"] += 1
        elif "likely_nonexistent_version_at_commit" in labels:
            outcomes["likely_commit"] += 1
        else:
            outcomes["no_supported_case_commit"] += 1
    return outcomes


def summarize(rows: list[dict[str, str]]) -> dict[str, Any]:
    labels = Counter(row["preliminary_label"] for row in rows)
    commits = commit_outcomes(rows)
    confirmed = labels["confirmed_nonexistent_version_at_commit"]
    inclusive = confirmed + labels["likely_nonexistent_version_at_commit"]
    total, commit_total = len(rows), sum(commits.values())
    return {
        "schema_version": SCHEMA_VERSION, "generated_utc": datetime.now(timezone.utc).isoformat(),
        "review_status": "preliminary_requires_human_confirmation",
        "case_total": total, "unique_commit_total": commit_total,
        "label_counts": dict(sorted(labels.items())),
        "language_counts": _nested_counts(rows, "language"),
        "agent_counts": _nested_counts(rows, "agent"),
        "commit_outcomes": dict(sorted(commits.items())),
        "case_precision_confirmed": _rate(confirmed, total),
        "case_precision_confirmed_or_likely": _rate(inclusive, total),
        "commit_precision_confirmed": _rate(commits["confirmed_commit"], commit_total),
        "commit_precision_confirmed_or_likely": _rate(commits["confirmed_commit"] + commits["likely_commit"], commit_total),
    }


def _nested_counts(rows: list[dict[str, str]], field: str) -> dict[str, dict[str, int]]:
    result: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        result[row[field]][row["preliminary_label"]] += 1
    return {key: dict(sorted(value.items())) for key, value in sorted(result.items())}


def _rate(successes: int, total: int) -> dict[str, Any]:
    low, high = wilson_interval(successes, total)
    return {
        "successes": successes, "total": total,
        "proportion": successes / total if total else 0.0,
        "wilson_95_low": low, "wilson_95_high": high,
    }


def report_markdown(summary: dict[str, Any]) -> str:
    labels = summary["label_counts"]
    confirmed = summary["case_precision_confirmed"]
    inclusive = summary["case_precision_confirmed_or_likely"]
    lines = [
        "# Preliminary strong-candidate validation review", "",
        "> Status: preliminary Codex evidence review; every case requires human confirmation.", "",
        "## Outcome", "",
        f"- Cases reviewed: {summary['case_total']} across {summary['unique_commit_total']} commits.",
        f"- Confirmed operational cases: {confirmed['successes']}/{confirmed['total']} ({_percent(confirmed['proportion'])}; Wilson 95% CI {_percent(confirmed['wilson_95_low'])} to {_percent(confirmed['wilson_95_high'])}).",
        f"- Confirmed + likely: {inclusive['successes']}/{inclusive['total']} ({_percent(inclusive['proportion'])}; Wilson 95% CI {_percent(inclusive['wilson_95_low'])} to {_percent(inclusive['wilson_95_high'])}).",
        "", "## Preliminary labels", "",
    ]
    lines.extend(f"- `{label}`: {count}" for label, count in sorted(labels.items()))
    lines.extend(["", "## Interpretation", "", "The original strong label has low case-level precision after first-parent, committer-time, first-party monorepo, alias-target, and root-history checks. A confirmed operational case proves only that the referenced public version was not yet published at the trusted commit time; it does not by itself prove that an AI agent caused a hallucination.", "", "Raw package names remain in the internal review table only. The public table uses stable redacted case IDs.", ""])
    return "\n".join(lines)


def _percent(value: float) -> str:
    return f"{100 * value:.2f}%"


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temporary, path)


def load_overrides(path: Path) -> dict[str, dict[str, str]]:
    with path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    if document.get("codebook_version") != CODEBOOK_VERSION:
        raise ValueError("override codebook version mismatch")
    return document["decisions"]


def write_outputs(root: Path, rows: list[dict[str, str]], summary: dict[str, Any]) -> None:
    public = [public_decision(row) for row in rows]
    invariants = validate_decisions(rows, expected=51)
    atomic_csv(root / "preliminary_codex_review.csv", rows, DECISION_FIELDS)
    atomic_csv(root / "public_redacted_preliminary_review.csv", public, PUBLIC_FIELDS)
    atomic_json(root / "preliminary_review_summary.json", summary)
    atomic_json(root / "preliminary_review_invariants.json", invariants)
    atomic_text(root / "preliminary_review_report.md", report_markdown(summary))
    outputs = [
        "preliminary_codex_review.csv", "public_redacted_preliminary_review.csv",
        "preliminary_review_summary.json", "preliminary_review_invariants.json",
        "preliminary_review_report.md",
    ]
    atomic_json(root / "preliminary_review_manifest.json", {
        "schema_version": SCHEMA_VERSION,
        "outputs": {name: fingerprint_file(root / name) for name in outputs},
    })


def main() -> None:
    args = parse_args()
    review_path = args.root / "review_form.csv"
    override_path = args.root / "preliminary_review_overrides.json"
    ledger_path = args.root / "case_specific_evidence_ledger.md"
    overrides = load_overrides(override_path)
    rows = [build_decision(row, overrides) for row in read_csv(review_path)]
    rows.sort(key=lambda row: (row["language"], row["repo"], row["sha"], row["case_id"]))
    summary = summarize(rows)
    summary["input_fingerprints"] = {
        "review_form": fingerprint_file(review_path),
        "overrides": fingerprint_file(override_path),
        "codebook": fingerprint_file(args.root / "validation_codebook.md"),
        "case_specific_evidence_ledger": fingerprint_file(ledger_path),
    }
    write_outputs(args.root, rows, summary)
    print(json.dumps(summary["label_counts"], sort_keys=True))


if __name__ == "__main__":
    main()

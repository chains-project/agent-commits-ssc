"""Finalize and audit overnight hallucinated-dependency results.

[IN]: per-language final_labels.csv and registry_lookup_results.csv from
run_refined_overnight.py output directories.
[OUT]: overnight_result_audit.md and overnight_result_audit.json.
[POS]: Completion audit for the refined Hallucinated Dependencies run.
[SYNC]: If output names change, update scripts/OUTPUTS.md and the project plan.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORRECT_EXCLUSION_REASONS = {"first_party_module_import", "private_or_local_dependency", "go_placeholder_version"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return list(csv.DictReader(line.replace("\x00", "") for line in handle))


def load_summaries(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in root.glob("wave*_n*/*/summary.json"):
        if path.parent.name == "C":
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            item["_summary_path"] = str(path)
            rows.append(item)
        except json.JSONDecodeError:
            continue
    return rows


def final_label_path(summary: dict[str, Any]) -> Path:
    return Path(summary["run_dir"]) / "manifest_registry" / "final_labels.csv"


def registry_path(summary: dict[str, Any]) -> Path:
    return Path(summary["run_dir"]) / "manifest_registry" / "registry_lookup_results.csv"


def audit(root: Path) -> dict[str, Any]:
    summaries = [row for row in load_summaries(root) if row.get("final_status") == "ok"]
    labels = Counter()
    reasons = Counter()
    by_language: dict[str, Counter] = defaultdict(Counter)
    by_wave: dict[str, Counter] = defaultdict(Counter)
    registry_buckets = Counter()
    unknown_rows = []
    invariant_failures = Counter()
    interpretation_categories = Counter()
    interpretation_by_language: dict[str, Counter] = defaultdict(Counter)
    total_final_rows = 0
    dedupe_keys = set()

    for summary in summaries:
        language = summary.get("language", "")
        wave_key = f"wave{summary.get('wave')}_n{summary.get('sample_size')}"
        final_rows = read_csv(final_label_path(summary))
        registry_rows = read_csv(registry_path(summary))
        total_final_rows += len(final_rows)
        for row in registry_rows:
            registry_buckets[row.get("registry_failure_bucket", "")] += 1
        for row in final_rows:
            label = row.get("final_label", "")
            category = interpretation_category(row)
            interpretation_categories[category] += 1
            interpretation_by_language[language][category] += 1
            labels[label] += 1
            by_language[language][label] += 1
            by_wave[wave_key][label] += 1
            if row.get("cannot_compare_reason"):
                reasons[row["cannot_compare_reason"]] += 1
            if label == "unknown_deleted_or_unpublished_possible":
                unknown_rows.append({
                    "wave": wave_key,
                    "language": language,
                    "repo": row.get("repo", ""),
                    "sha": row.get("sha", ""),
                    "declared_package": row.get("declared_package", ""),
                    "query_package": row.get("query_package", ""),
                    "query_version": row.get("query_version", ""),
                    "registry_ecosystem": row.get("registry_ecosystem", ""),
                    "package_exists": row.get("registry_package_exists_current_registry", ""),
                    "version_exists": row.get("registry_version_exists_current_registry", ""),
                    "reason": row.get("label_reason", ""),
                })
            if label == "cannot_compare" and not row.get("cannot_compare_reason"):
                invariant_failures["cannot_compare_missing_reason"] += 1
            if label != "cannot_compare" and row.get("cannot_compare_reason"):
                invariant_failures["non_cannot_with_cannot_reason"] += 1
            if label == "version_exists_before_author_date" and str(row.get("registry_version_exists_current_registry", "")).lower() != "true":
                invariant_failures["strong_label_missing_version_evidence"] += 1
            if label != "cannot_compare" and row.get("registry_registry_failure_bucket") in {"network_or_decode_error", "registry_server_error", "rate_limited"}:
                invariant_failures["registry_failure_promoted"] += 1
            key = (
                row.get("repo", ""),
                row.get("sha", ""),
                row.get("declared_package", ""),
                row.get("query_package", ""),
                row.get("query_version", ""),
                row.get("registry_ecosystem", ""),
                label,
                row.get("label_reason", ""),
            )
            dedupe_keys.add(key)

    result = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "completed_runs": len(summaries),
        "total_final_rows": total_final_rows,
        "dedup_repo_commit_package_version_label_rows": len(dedupe_keys),
        "final_labels": dict(labels),
        "cannot_compare_reasons": dict(reasons),
        "registry_failure_buckets": dict(registry_buckets),
        "interpretation_categories": dict(interpretation_categories),
        "interpretation_categories_by_language": {key: dict(value) for key, value in interpretation_by_language.items()},
        "final_labels_by_language": {key: dict(value) for key, value in by_language.items()},
        "final_labels_by_wave": {key: dict(value) for key, value in by_wave.items()},
        "unknown_rows": unknown_rows[:200],
        "unknown_rows_total": len(unknown_rows),
        "invariant_failures": dict(invariant_failures),
    }
    return result


def interpretation_category(row: dict[str, str]) -> str:
    label = row.get("final_label", "")
    reason = row.get("cannot_compare_reason", "")
    if label == "version_exists_before_author_date":
        return "evidence_resolved_exists_before_author_date"
    if label == "unknown_deleted_or_unpublished_possible":
        return "weak_unknown_current_registry_missing"
    if label == "cannot_compare" and reason in CORRECT_EXCLUSION_REASONS:
        return "excluded_first_party_private_or_placeholder"
    if label == "cannot_compare":
        return "unresolved_cannot_compare"
    return label or "unclassified"


def write_markdown(root: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Overnight Result Audit",
        "",
        f"- Generated UTC: {result['generated_utc']}",
        f"- Completed language/sample runs: {result['completed_runs']}",
        f"- Event-level final rows: {result['total_final_rows']:,}",
        f"- Deduplicated repo/commit/package/version/label rows: {result['dedup_repo_commit_package_version_label_rows']:,}",
        f"- Unknown rows: {result['unknown_rows_total']:,}",
        "",
        "## Final Labels",
        "",
        "| Label | Rows |",
        "|---|---:|",
    ]
    for label, count in Counter(result["final_labels"]).most_common():
        lines.append(f"| `{label}` | {count:,} |")

    lines.extend(["", "## Interpretation Categories", "", "| Category | Rows |", "|---|---:|"])
    for category, count in Counter(result["interpretation_categories"]).most_common():
        lines.append(f"| `{category}` | {count:,} |")

    lines.extend(["", "## Cannot Compare Reasons", "", "| Reason | Rows |", "|---|---:|"])
    for reason, count in Counter(result["cannot_compare_reasons"]).most_common():
        lines.append(f"| `{reason}` | {count:,} |")

    lines.extend(["", "## Invariant Checks", "", "| Check | Failures |", "|---|---:|"])
    failures = Counter(result["invariant_failures"])
    if failures:
        for key, count in failures.most_common():
            lines.append(f"| `{key}` | {count:,} |")
    else:
        lines.append("| all_checked_invariants | 0 |")

    lines.extend(["", "## Interpretation Categories By Language", "", "| Language | Category | Rows |", "|---|---|---:|"])
    for language, values in sorted(result["interpretation_categories_by_language"].items()):
        for category, count in Counter(values).most_common():
            lines.append(f"| `{language}` | `{category}` | {count:,} |")

    lines.extend(["", "## Labels By Language", "", "| Language | Label | Rows |", "|---|---|---:|"])
    for language, values in sorted(result["final_labels_by_language"].items()):
        for label, count in Counter(values).most_common():
            lines.append(f"| `{language}` | `{label}` | {count:,} |")

    lines.extend(["", "## Unknown Rows Sample", "", "| Wave | Language | Repo | Package | Version | Evidence | Reason |", "|---|---|---|---|---|---|---|"])
    for row in result["unknown_rows"][:50]:
        evidence = f"package={row['package_exists']}; version={row['version_exists']}"
        package = row.get("query_package") or row.get("declared_package")
        lines.append(f"| `{row['wave']}` | `{row['language']}` | `{row['repo']}` | `{package}` | `{row['query_version']}` | `{evidence}` | `{row['reason']}` |")

    lines.extend([
        "",
        "## Interpretation",
        "",
        "- `unknown_deleted_or_unpublished_possible` remains weak current-registry evidence and needs row-level audit.",
        "- Strong non-hallucination labels require exact current-registry version evidence and publish time before/equal to commit author date.",
        "- Network failures and mapping uncertainty remain conservative `cannot_compare` cases.",
        "",
    ])
    (root / "overnight_result_audit.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    result = audit(args.output_root)
    with (args.output_root / "overnight_result_audit.json").open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    write_markdown(args.output_root, result)


if __name__ == "__main__":
    main()

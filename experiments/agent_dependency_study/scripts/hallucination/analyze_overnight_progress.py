"""Analyze overnight hallucination experiment progress.

[IN]: status.csv and per-language summary.json files from run_refined_overnight.py.
[OUT]: progress_analysis.json and progress_analysis.md in the output root.
[POS]: Periodic local analysis for the refined Hallucinated Dependencies run.
[SYNC]: If output names change, update scripts/OUTPUTS.md and the project plan.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LANGUAGES = ["TypeScript", "Python", "JavaScript", "C#", "Rust", "Go", "PHP", "Java", "C++", "Kotlin"]
WAVES = [(1, 30), (2, 50), (3, 100), (4, 200)]
SUCCESS_STATUSES = {"", "ok", "passed"}


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
            rows.append(json.loads(path.read_text(encoding="utf-8", errors="replace")))
        except json.JSONDecodeError:
            continue
    return rows


def stage3_key(row: dict[str, str]) -> tuple[int, int, str]:
    return int(row.get("wave") or 0), int(row.get("sample_size") or 0), row.get("language", "")


def status_key(row: dict[str, str]) -> tuple[int, int, str, str]:
    return (
        int(row.get("wave") or 0),
        int(row.get("sample_size") or 0),
        row.get("language", ""),
        row.get("stage", ""),
    )


def analyze(root: Path) -> dict[str, Any]:
    status_rows = read_csv(root / "status.csv")
    summaries = load_summaries(root)
    completed = {stage3_key(row) for row in status_rows if row.get("stage") == "stage3_registry_label" and row.get("status") == "ok"}
    latest_by_stage = {}
    for row in status_rows:
        latest_by_stage[status_key(row)] = row
    non_ok = [row for row in status_rows if row.get("status") not in SUCCESS_STATUSES]
    active_failures = {status_key(row) for row in latest_by_stage.values() if row.get("status") not in SUCCESS_STATUSES}
    unsuperseded_failures = [row for row in non_ok if status_key(row) in active_failures and stage3_key(row) not in completed]
    superseded_failures = [row for row in non_ok if status_key(row) not in active_failures or stage3_key(row) in completed]

    matrix = []
    for wave, sample_size in WAVES:
        done = [language for language in LANGUAGES if (wave, sample_size, language) in completed]
        pending = [language for language in LANGUAGES if language not in done]
        matrix.append({
            "wave": wave,
            "sample_size": sample_size,
            "completed_count": len(done),
            "completed_languages": done,
            "pending_languages": pending,
        })

    labels = Counter()
    reasons = Counter()
    buckets = Counter()
    totals = Counter()
    for item in summaries:
        if item.get("final_status") != "ok":
            continue
        labels.update(item.get("final_labels", {}))
        reasons.update(item.get("cannot_compare_reasons", {}))
        buckets.update(item.get("registry_failure_buckets", {}))
        for key in ["candidate_commits", "import_events", "import_dependency_matches", "registry_lookup_rows", "final_label_rows"]:
            totals[key] += int(item.get(key) or 0)

    actions = []
    if unsuperseded_failures:
        actions.append("There are unsuperseded failed stages; resume or rerun those language/sample runs before trusting aggregate totals.")
    else:
        actions.append("No unsuperseded failed stages; historical failures have either been fixed or are not part of current aggregation.")
    if reasons.get("mapping_uncertain", 0) > reasons.get("no_manifest_or_no_parsed_dependencies", 0):
        actions.append("Dominant bottleneck is mapping_uncertain; next code optimization should target language-specific import-to-package mapping, not registry lookup.")
    if reasons.get("no_manifest_or_no_parsed_dependencies", 0) > 0:
        actions.append("Manifest discovery remains a major bottleneck; inspect languages with high no_manifest_or_no_parsed_dependencies after the run.")
    if buckets.get("network_or_decode_error", 0) or buckets.get("registry_server_error", 0):
        actions.append("Registry failures exist but remain conservative labels; retry cache/mirror can be a follow-up, not a reason to stop current run.")
    if labels.get("unknown_deleted_or_unpublished_possible", 0):
        actions.append("Unknown labels require post-run row-level audit; treat them as weak current-registry evidence.")

    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "status_rows": len(status_rows),
        "summary_runs": len(summaries),
        "completed_stage3_count": len(completed),
        "wave_matrix": matrix,
        "non_ok_rows": len(non_ok),
        "superseded_failures": superseded_failures,
        "unsuperseded_failures": unsuperseded_failures,
        "totals": dict(totals),
        "final_labels": dict(labels),
        "cannot_compare_reasons": dict(reasons),
        "registry_failure_buckets": dict(buckets),
        "actions": actions,
    }


def write_markdown(root: Path, result: dict[str, Any]) -> None:
    lines = [
        "# Overnight Progress Analysis",
        "",
        f"- Generated UTC: {result['generated_utc']}",
        f"- Status rows: {result['status_rows']}",
        f"- Summary runs: {result['summary_runs']}",
        f"- Completed stage3 language/sample runs: {result['completed_stage3_count']}",
        f"- Non-ok historical rows: {result['non_ok_rows']}",
        f"- Unsuperseded failures: {len(result['unsuperseded_failures'])}",
        f"- Superseded failures: {len(result['superseded_failures'])}",
        "",
        "## Wave Matrix",
        "",
        "| Wave | Sample | Completed | Done | Pending |",
        "|---:|---:|---:|---|---|",
    ]
    for row in result["wave_matrix"]:
        done = ", ".join(row["completed_languages"]) or "-"
        pending = ", ".join(row["pending_languages"]) or "-"
        lines.append(f"| {row['wave']} | {row['sample_size']} | {row['completed_count']}/10 | {done} | {pending} |")

    lines.extend(["", "## Final Labels", "", "| Label | Rows |", "|---|---:|"])
    for label, count in Counter(result["final_labels"]).most_common():
        lines.append(f"| `{label}` | {count:,} |")

    lines.extend(["", "## Cannot Compare Reasons", "", "| Reason | Rows |", "|---|---:|"])
    for reason, count in Counter(result["cannot_compare_reasons"]).most_common():
        lines.append(f"| `{reason}` | {count:,} |")

    lines.extend(["", "## Automated Assessment", ""])
    for action in result["actions"]:
        lines.append(f"- {action}")

    if result["unsuperseded_failures"]:
        lines.extend(["", "## Unsuperseded Failures", "", "| Wave | Sample | Language | Stage | Status | Timestamp |", "|---:|---:|---|---|---|---|"])
        for row in result["unsuperseded_failures"]:
            lines.append(f"| {row.get('wave')} | {row.get('sample_size')} | `{row.get('language')}` | `{row.get('stage')}` | `{row.get('status')}` | {row.get('timestamp')} |")

    (root / "progress_analysis.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    result = analyze(args.output_root)
    with (args.output_root / "progress_analysis.json").open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    write_markdown(args.output_root, result)


if __name__ == "__main__":
    main()

"""Audit completed waves in the overnight hallucination experiment.

[IN]: per-language final_labels.csv and registry_lookup_results.csv from
run_refined_overnight.py output directories.
[OUT]: wave_audits/waveXX_nYY_audit.md/json and sampled_label_rows.csv.
[POS]: Per-wave checkpoint audit for label quality and pipeline health.
[SYNC]: If output names change, update scripts/OUTPUTS.md and the project plan.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LANGUAGES = ["TypeScript", "Python", "JavaScript", "C#", "Rust", "Go", "PHP", "Java", "C++", "Kotlin"]
LANG_DIRS = {"C#": "CSharp", "C++": "Cpp"}
WAVES = [(1, 30), (2, 50), (3, 100), (4, 200)]
WAVE_DIR_RE = re.compile(r"^wave(?P<wave>\d+)_n(?P<sample>\d+)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--include-incomplete", action="store_true")
    parser.add_argument("--sample-per-bucket", type=int, default=5)
    parser.add_argument(
        "--waves",
        nargs="*",
        help="Optional wave/sample pairs like 3:100. Defaults to discovering output dirs.",
    )
    return parser.parse_args()


def lang_dir(language: str) -> str:
    return LANG_DIRS.get(language, language)


def parse_wave_arg(value: str) -> tuple[int, int]:
    left, sep, right = value.partition(":")
    if sep != ":":
        raise argparse.ArgumentTypeError("wave must be formatted as WAVE:SAMPLE, for example 3:100")
    return int(left), int(right)


def discover_waves(root: Path, requested: list[str] | None) -> list[tuple[int, int]]:
    if requested:
        return sorted(parse_wave_arg(value) for value in requested)
    discovered = []
    for path in root.glob("wave*_n*"):
        if not path.is_dir():
            continue
        match = WAVE_DIR_RE.match(path.name)
        if match:
            discovered.append((int(match.group("wave")), int(match.group("sample"))))
    return sorted(set(discovered)) or WAVES


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return list(csv.DictReader(line.replace("\x00", "") for line in handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row}) if rows else ["message"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def completed_languages(root: Path, wave: int, sample_size: int) -> list[str]:
    done = []
    for language in LANGUAGES:
        summary_path = root / f"wave{wave:02d}_n{sample_size}" / lang_dir(language) / "summary.json"
        if not summary_path.exists():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if summary.get("final_status") == "ok":
            done.append(language)
    return done


def load_wave_rows(root: Path, wave: int, sample_size: int, languages: list[str]) -> list[dict[str, str]]:
    rows = []
    for language in languages:
        path = root / f"wave{wave:02d}_n{sample_size}" / lang_dir(language) / "manifest_registry" / "final_labels.csv"
        for row in read_csv(path):
            row["_wave"] = f"wave{wave:02d}_n{sample_size}"
            row["_language"] = language
            rows.append(row)
    return rows


def sample_rows(rows: list[dict[str, str]], sample_per_bucket: int) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        label = row.get("final_label", "")
        reason = row.get("cannot_compare_reason") or row.get("label_reason", "")
        buckets[(label, reason)].append(row)
    sampled = []
    for (label, reason), bucket_rows in sorted(buckets.items()):
        for row in bucket_rows[:sample_per_bucket]:
            sampled.append({
                "wave": row.get("_wave", ""),
                "language": row.get("_language", ""),
                "final_label": label,
                "reason": reason,
                "repo": row.get("repo", ""),
                "sha": row.get("sha", ""),
                "source_file": row.get("source_file", ""),
                "import_raw": row.get("import_raw", ""),
                "package_candidate": row.get("package_candidate", ""),
                "declared_package": row.get("declared_package", ""),
                "query_package": row.get("query_package", ""),
                "query_version": row.get("query_version", ""),
                "registry_ecosystem": row.get("registry_ecosystem", ""),
                "registry_source": row.get("registry_registry_source", ""),
                "package_exists": row.get("registry_package_exists_current_registry", ""),
                "version_exists": row.get("registry_version_exists_current_registry", ""),
                "publish_time": row.get("registry_publish_time", ""),
                "author_date": row.get("author_date", ""),
                "declared_dependency_match": row.get("declared_dependency_match", ""),
                "version_kind": row.get("version_kind", ""),
            })
    return sampled


def audit_wave(root: Path, wave: int, sample_size: int, include_incomplete: bool, sample_per_bucket: int) -> dict[str, Any] | None:
    done = completed_languages(root, wave, sample_size)
    if len(done) < len(LANGUAGES) and not include_incomplete:
        return None
    rows = load_wave_rows(root, wave, sample_size, done)
    labels = Counter(row.get("final_label", "") for row in rows)
    reasons = Counter(row.get("cannot_compare_reason", "") for row in rows if row.get("cannot_compare_reason"))
    by_language: dict[str, Counter] = defaultdict(Counter)
    invariant_failures = Counter()
    for row in rows:
        label = row.get("final_label", "")
        by_language[row.get("_language", "")][label] += 1
        if label == "cannot_compare" and not row.get("cannot_compare_reason"):
            invariant_failures["cannot_compare_missing_reason"] += 1
        if label != "cannot_compare" and row.get("cannot_compare_reason"):
            invariant_failures["non_cannot_with_cannot_reason"] += 1
        if label == "version_exists_before_author_date" and str(row.get("registry_version_exists_current_registry", "")).lower() != "true":
            invariant_failures["strong_label_missing_version_evidence"] += 1
        if label != "cannot_compare" and row.get("registry_registry_failure_bucket") in {"network_or_decode_error", "registry_server_error", "rate_limited"}:
            invariant_failures["registry_failure_promoted"] += 1

    sampled = sample_rows(rows, sample_per_bucket)
    audit = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "wave": wave,
        "sample_size": sample_size,
        "completed_languages": done,
        "completed_count": len(done),
        "event_rows": len(rows),
        "final_labels": dict(labels),
        "cannot_compare_reasons": dict(reasons),
        "final_labels_by_language": {key: dict(value) for key, value in by_language.items()},
        "invariant_failures": dict(invariant_failures),
        "sampled_rows_count": len(sampled),
        "sampled_rows_csv": f"wave_audits/wave{wave:02d}_n{sample_size}_sampled_label_rows.csv",
    }

    out_dir = root / "wave_audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / f"wave{wave:02d}_n{sample_size}"
    with (prefix.with_name(prefix.name + "_audit.json")).open("w", encoding="utf-8") as handle:
        json.dump(audit, handle, ensure_ascii=False, indent=2)
    write_csv(prefix.with_name(prefix.name + "_sampled_label_rows.csv"), sampled)
    write_markdown(prefix.with_name(prefix.name + "_audit.md"), audit, sampled)
    return audit


def write_markdown(path: Path, audit: dict[str, Any], sampled: list[dict[str, Any]]) -> None:
    lines = [
        f"# Wave {audit['wave']:02d} n={audit['sample_size']} Label Audit",
        "",
        f"- Generated UTC: {audit['generated_utc']}",
        f"- Completed languages: {audit['completed_count']}/10",
        f"- Event-level final rows: {audit['event_rows']:,}",
        f"- Sampled label rows CSV: `{audit['sampled_rows_csv']}`",
        "",
        "## Final Labels",
        "",
        "| Label | Rows |",
        "|---|---:|",
    ]
    for label, count in Counter(audit["final_labels"]).most_common():
        lines.append(f"| `{label}` | {count:,} |")
    lines.extend(["", "## Cannot Compare Reasons", "", "| Reason | Rows |", "|---|---:|"])
    for reason, count in Counter(audit["cannot_compare_reasons"]).most_common():
        lines.append(f"| `{reason}` | {count:,} |")
    lines.extend(["", "## Invariant Checks", "", "| Check | Failures |", "|---|---:|"])
    failures = Counter(audit["invariant_failures"])
    if failures:
        for key, count in failures.most_common():
            lines.append(f"| `{key}` | {count:,} |")
    else:
        lines.append("| all_checked_invariants | 0 |")
    lines.extend(["", "## Sampled Rows For Manual Review", "", "| Label | Reason | Language | Repo | Import | Package | Version | Evidence |", "|---|---|---|---|---|---|---|---|"])
    for row in sampled[:80]:
        package = row.get("query_package") or row.get("declared_package") or row.get("package_candidate")
        evidence = f"{row.get('registry_source')}; pkg={row.get('package_exists')}; ver={row.get('version_exists')}; pub={row.get('publish_time')}"
        lines.append(f"| `{row.get('final_label')}` | `{row.get('reason')}` | `{row.get('language')}` | `{row.get('repo')}` | `{row.get('import_raw')}` | `{package}` | `{row.get('query_version')}` | `{evidence}` |")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    audits = []
    out_dir = args.output_root / "wave_audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    for wave, sample_size in discover_waves(args.output_root, args.waves):
        result = audit_wave(args.output_root, wave, sample_size, args.include_incomplete, args.sample_per_bucket)
        if result:
            audits.append(result)
    with (out_dir / "wave_audit_index.json").open("w", encoding="utf-8") as handle:
        json.dump({"generated_utc": datetime.now(timezone.utc).isoformat(), "audits": audits}, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

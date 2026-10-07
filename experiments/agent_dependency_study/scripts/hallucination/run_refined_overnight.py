"""Run refined hallucinated-dependency experiments overnight.

[IN]: local commit JSON diff index plus merged agent population consumed by
hallucinated_dependency_probe.py.
[OUT]: per-language stage outputs, logs, status.csv, summary.json, and
experiment_report.md under the selected output root.
[POS]: Orchestrates the full refined Hallucinated Dependencies pipeline.
[SYNC]: If output paths or report files change, update scripts/OUTPUTS.md and
.claude/plans/2026-07-07-hallucination-full-pipeline.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PYTHON = Path(r"C:\ProgramData\Anaconda3\python.exe")
LANGUAGES = ["TypeScript", "Python", "JavaScript", "C#", "Rust", "Go", "PHP", "Java", "C++", "Kotlin"]
SAMPLE_SIZES = [30, 50, 100, 200]
ROOT = Path("data_products/rq2_hallucinated_overnight_refined_20260707")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=ROOT)
    parser.add_argument("--languages", nargs="*", default=LANGUAGES)
    parser.add_argument("--sample-sizes", nargs="*", type=int, default=SAMPLE_SIZES)
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument("--time-budget-hours", type=float, default=7.75)
    parser.add_argument("--random-seed", type=int, default=20260707)
    parser.add_argument("--manifest-sleep-seconds", type=float, default=0.1)
    parser.add_argument("--tree-max-files", type=int, default=24)
    parser.add_argument("--registry-sleep-seconds", type=float, default=0.15)
    parser.add_argument("--registry-retry-count", type=int, default=2)
    parser.add_argument("--registry-retry-sleep-seconds", type=float, default=2.0)
    parser.add_argument("--stage1-timeout-minutes", type=int, default=45)
    parser.add_argument("--manifest-timeout-minutes", type=int, default=120)
    parser.add_argument("--registry-timeout-minutes", type=int, default=60)
    parser.add_argument("--disable-wave-quality-gate", action="store_true")
    return parser.parse_args()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str) -> str:
    special = {"C#": "CSharp", "C++": "Cpp", "F#": "FSharp"}
    if value in special:
        return special[value]
    out = []
    for char in value:
        out.append(char if char.isalnum() else "_")
    return "".join(out).strip("_") or "_"


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return list(csv.DictReader(line.replace("\x00", "") for line in handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = sorted({key for row in rows for key in row}) if rows else ["timestamp", "message"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def append_status(root: Path, row: dict[str, Any]) -> None:
    path = root / "status.csv"
    fields = [
        "timestamp", "wave", "language", "sample_size", "stage", "status", "returncode",
        "start_utc", "end_utc", "duration_seconds", "output_dir", "log_path", "message",
    ]
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in fields})


def run_command(cmd: list[str], timeout_minutes: int, log_path: Path) -> tuple[int, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = now()
    with log_path.open("w", encoding="utf-8", newline="") as log:
        log.write("start_utc=" + start + "\n")
        log.write("command=" + " ".join(cmd) + "\n\n")
        log.flush()
        try:
            proc = subprocess.run(
                cmd,
                cwd=Path.cwd(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_minutes * 60,
            )
            log.write(proc.stdout or "")
            log.write("\nend_utc=" + now() + "\n")
            log.write("returncode=" + str(proc.returncode) + "\n")
            return proc.returncode, "ok" if proc.returncode == 0 else "command_failed"
        except subprocess.TimeoutExpired as exc:
            log.write(exc.stdout or "")
            log.write("\nend_utc=" + now() + "\n")
            log.write("timeout_minutes=" + str(timeout_minutes) + "\n")
            return 124, "timeout"
        except Exception as exc:  # keep orchestrator alive across one language failure
            log.write("\nexception=" + repr(exc) + "\n")
            return 1, "exception"


def pipeline_for_language(args: argparse.Namespace, language: str, sample_size: int, wave: int) -> dict[str, Any]:
    root = args.output_root
    lang_key = safe_name(language)
    run_dir = root / f"wave{wave:02d}_n{sample_size}" / lang_key
    stage1_dir = run_dir / "stage1"
    stage2_dir = run_dir / "manifest_registry"
    logs = root / "logs" / f"wave{wave:02d}_n{sample_size}" / lang_key
    existing_summary = run_dir / "summary.json"
    if existing_summary.exists():
        try:
            summary = json.loads(existing_summary.read_text(encoding="utf-8", errors="replace"))
            if summary.get("final_status") == "ok":
                append_status(root, {
                    "timestamp": now(),
                    "wave": wave,
                    "language": language,
                    "sample_size": sample_size,
                    "stage": "resume_skip_completed",
                    "status": "ok",
                    "returncode": 0,
                    "output_dir": str(run_dir),
                    "message": "existing successful summary reused",
                })
                return summary
        except json.JSONDecodeError:
            pass
    stages = [
        (
            "stage1_import_extraction",
            [
                str(PYTHON), "scripts/hallucination/hallucinated_dependency_probe.py",
                "--languages", language,
                "--sample-per-language", str(sample_size),
                "--sampling-mode", "first",
                "--random-seed", str(args.random_seed),
                "--output-dir", str(stage1_dir),
            ],
            args.stage1_timeout_minutes,
            stage1_dir,
        ),
        (
            "stage2_manifest_enrichment",
            [
                str(PYTHON), "scripts/hallucination/manifest_enrich.py",
                "--input-dir", str(stage1_dir),
                "--output-dir", str(stage2_dir),
                "--fetch-tree",
                "--tree-mode", "merge-tree",
                "--tree-max-files", str(args.tree_max_files),
                "--sleep-seconds", str(args.manifest_sleep_seconds),
            ],
            args.manifest_timeout_minutes,
            stage2_dir,
        ),
        (
            "stage3_registry_label",
            [
                str(PYTHON), "scripts/hallucination/registry_label.py",
                "--input-dir", str(stage2_dir),
                "--output-dir", str(stage2_dir),
                "--timeout", "30",
                "--sleep-seconds", str(args.registry_sleep_seconds),
                "--retry-count", str(args.registry_retry_count),
                "--retry-sleep-seconds", str(args.registry_retry_sleep_seconds),
            ],
            args.registry_timeout_minutes,
            stage2_dir,
        ),
    ]
    summary: dict[str, Any] = {"language": language, "sample_size": sample_size, "wave": wave, "run_dir": str(run_dir)}
    for stage_name, cmd, timeout_minutes, out_dir in stages:
        start = time.time()
        start_utc = now()
        log_path = logs / f"{stage_name}.log"
        returncode, status = run_command(cmd, timeout_minutes, log_path)
        end_utc = now()
        duration = round(time.time() - start, 2)
        append_status(root, {
            "timestamp": end_utc,
            "wave": wave,
            "language": language,
            "sample_size": sample_size,
            "stage": stage_name,
            "status": status,
            "returncode": returncode,
            "start_utc": start_utc,
            "end_utc": end_utc,
            "duration_seconds": duration,
            "output_dir": str(out_dir),
            "log_path": str(log_path),
            "message": "",
        })
        summary[stage_name] = {"status": status, "returncode": returncode, "duration_seconds": duration, "log": str(log_path)}
        write_interim_report(root, args)
        if returncode != 0:
            summary["final_status"] = "failed_at_" + stage_name
            write_language_summary(run_dir, summary)
            return summary
    summary.update(collect_run_counts(stage1_dir, stage2_dir))
    summary["final_status"] = "ok"
    write_language_summary(run_dir, summary)
    return summary


def collect_run_counts(stage1_dir: Path, stage2_dir: Path) -> dict[str, Any]:
    final_rows = read_csv(stage2_dir / "final_labels.csv")
    registry_rows = read_csv(stage2_dir / "registry_lookup_results.csv")
    match_rows = read_csv(stage2_dir / "import_dependency_matches.csv")
    candidate_rows = read_csv(stage1_dir / "candidate_commits.csv")
    import_rows = read_csv(stage1_dir / "import_events.csv")
    labels = Counter(row.get("final_label", "") for row in final_rows)
    reasons = Counter(row.get("cannot_compare_reason", "") for row in final_rows if row.get("cannot_compare_reason"))
    registry_buckets = Counter(row.get("registry_failure_bucket", "") for row in registry_rows)
    return {
        "candidate_commits": len(candidate_rows),
        "import_events": len(import_rows),
        "import_dependency_matches": len(match_rows),
        "registry_lookup_rows": len(registry_rows),
        "final_label_rows": len(final_rows),
        "final_labels": dict(labels),
        "cannot_compare_reasons": dict(reasons),
        "registry_failure_buckets": dict(registry_buckets),
    }


def write_language_summary(run_dir: Path, summary: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)


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


def write_interim_report(root: Path, args: argparse.Namespace) -> None:
    summaries = load_summaries(root)
    report = build_report(root, args, summaries, final=False)
    (root / "overnight_interim_report.md").write_text(report, encoding="utf-8")


def write_final_report(root: Path, args: argparse.Namespace) -> None:
    summaries = load_summaries(root)
    report = build_report(root, args, summaries, final=True)
    (root / "overnight_experiment_report.md").write_text(report, encoding="utf-8")
    with (root / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump({"runs": summaries}, handle, ensure_ascii=False, indent=2)


def run_wave_audit(root: Path, wave: int, sample_size: int) -> dict[str, Any]:
    log_path = root / "wave_audits" / f"wave{wave:02d}_n{sample_size}_gate_audit.log"
    cmd = [
        str(PYTHON),
        "scripts/hallucination/audit_wave_results.py",
        "--output-root",
        str(root),
        "--waves",
        f"{wave}:{sample_size}",
    ]
    returncode, status = run_command(cmd, 15, log_path)
    append_status(root, {
        "timestamp": now(),
        "wave": wave,
        "sample_size": sample_size,
        "stage": "wave_audit",
        "status": status,
        "returncode": returncode,
        "output_dir": str(root / "wave_audits"),
        "log_path": str(log_path),
    })
    audit_path = root / "wave_audits" / f"wave{wave:02d}_n{sample_size}_audit.json"
    if returncode != 0 or not audit_path.exists():
        return {"status": "blocked", "reasons": ["wave_audit_failed_or_missing"], "audit_path": str(audit_path)}
    try:
        return json.loads(audit_path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {"status": "blocked", "reasons": ["wave_audit_json_decode_failed"], "audit_path": str(audit_path)}


def load_wave_final_rows(root: Path, wave: int, sample_size: int, languages: list[str]) -> list[dict[str, str]]:
    rows = []
    for language in languages:
        path = root / f"wave{wave:02d}_n{sample_size}" / safe_name(language) / "manifest_registry" / "final_labels.csv"
        for row in read_csv(path):
            row["_language"] = language
            rows.append(row)
    return rows


def is_android_maven_unknown(row: dict[str, str]) -> bool:
    package = row.get("query_package") or row.get("declared_package") or row.get("package_candidate") or ""
    source = row.get("registry_registry_source", "")
    if row.get("final_label") != "unknown_deleted_or_unpublished_possible":
        return False
    if row.get("registry_ecosystem") != "Maven" or source != "search.maven.org":
        return False
    return package.startswith(("androidx.", "com.android.", "com.google.android."))


def evaluate_wave_gate(root: Path, wave: int, sample_size: int, audit: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    invariant_failures = audit.get("invariant_failures") or {}
    if any(int(value) > 0 for value in invariant_failures.values()):
        reasons.append("invariant_failures_present")
    languages = audit.get("completed_languages") or []
    if len(languages) < len(LANGUAGES):
        reasons.append("wave_incomplete")
    final_rows = load_wave_final_rows(root, wave, sample_size, languages)
    android_maven_unknowns = [row for row in final_rows if is_android_maven_unknown(row)]
    if android_maven_unknowns:
        reasons.append("android_maven_unknowns_need_lookup_fix")
    decision = {
        "generated_utc": now(),
        "wave": wave,
        "sample_size": sample_size,
        "status": "blocked" if reasons else "passed",
        "reasons": reasons,
        "completed_languages": languages,
        "event_rows": audit.get("event_rows", 0),
        "invariant_failures": invariant_failures,
        "android_maven_unknown_rows": len(android_maven_unknowns),
        "manual_review_required": True,
        "next_action": "patch_and_rerun_before_next_wave" if reasons else "manual_sample_review_then_next_wave",
    }
    write_wave_gate_decision(root, decision)
    append_status(root, {
        "timestamp": now(),
        "wave": wave,
        "sample_size": sample_size,
        "stage": "wave_quality_gate",
        "status": decision["status"],
        "returncode": 1 if reasons else 0,
        "output_dir": str(root / "wave_audits"),
        "message": ";".join(reasons),
    })
    return decision


def write_wave_gate_decision(root: Path, decision: dict[str, Any]) -> None:
    out_dir = root / "wave_audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / f"wave{decision['wave']:02d}_n{decision['sample_size']}_gate"
    with (prefix.with_suffix(".json")).open("w", encoding="utf-8") as handle:
        json.dump(decision, handle, ensure_ascii=False, indent=2)
    lines = [
        f"# Wave {decision['wave']:02d} n={decision['sample_size']} Quality Gate",
        "",
        f"- Generated UTC: {decision['generated_utc']}",
        f"- Status: `{decision['status']}`",
        f"- Event rows: {decision['event_rows']:,}",
        f"- Completed languages: {len(decision['completed_languages'])}/{len(LANGUAGES)}",
        f"- Android Maven unknown rows: {decision['android_maven_unknown_rows']:,}",
        f"- Manual review required: `{decision['manual_review_required']}`",
        f"- Next action: `{decision['next_action']}`",
        "",
        "## Blocking Reasons",
        "",
    ]
    if decision["reasons"]:
        lines.extend(f"- `{reason}`" for reason in decision["reasons"])
    else:
        lines.append("- none")
    (prefix.with_suffix(".md")).write_text("\n".join(lines), encoding="utf-8")


def build_report(root: Path, args: argparse.Namespace, summaries: list[dict[str, Any]], final: bool) -> str:
    status_rows = read_csv(root / "status.csv")
    labels = Counter()
    reasons = Counter()
    buckets = Counter()
    ok_runs = 0
    failed_runs = 0
    for item in summaries:
        if item.get("final_status") == "ok":
            ok_runs += 1
        else:
            failed_runs += 1
        labels.update(item.get("final_labels", {}))
        reasons.update(item.get("cannot_compare_reasons", {}))
        buckets.update(item.get("registry_failure_buckets", {}))
    lines = [
        "# Refined Hallucinated Dependencies Overnight Experiment",
        "",
        f"Report type: {'final' if final else 'interim'}",
        f"Output root: `{root}`",
        f"Generated UTC: {now()}",
        "",
        "## Purpose",
        "",
        "Run the refined pipeline by language, in parallel, to split `cannot_compare` into more precise statuses while keeping hallucination labels conservative.",
        "",
        "## Configuration",
        "",
        f"- Languages: {', '.join(args.languages)}",
        f"- Sample sizes by wave: {', '.join(str(x) for x in args.sample_sizes)}",
        f"- Max workers: {args.max_workers}",
        f"- Time budget hours: {args.time_budget_hours}",
        "- Pipeline: import extraction -> child manifest/lockfile enrichment -> registry/advisory labeling",
        "",
        "## Run Status",
        "",
        f"- Completed language/sample runs: {ok_runs}",
        f"- Failed or partial language/sample runs: {failed_runs}",
        f"- Stage status rows: {len(status_rows)}",
        "",
        "## Final Labels Aggregated Over Completed Runs",
        "",
        "| Label | Rows |",
        "|---|---:|",
    ]
    for label, count in labels.most_common():
        lines.append(f"| `{label or 'blank'}` | {count:,} |")
    lines.extend(["", "## Cannot Compare Reasons", "", "| Reason | Rows |", "|---|---:|"])
    for reason, count in reasons.most_common():
        lines.append(f"| `{reason or 'blank'}` | {count:,} |")
    lines.extend(["", "## Registry Failure Buckets", "", "| Bucket | Rows |", "|---|---:|"])
    for bucket, count in buckets.most_common():
        lines.append(f"| `{bucket or 'blank'}` | {count:,} |")
    lines.extend(["", "## Per-Run Summary", "", "| Wave | Sample | Language | Status | Candidates | Imports | Matches | Registry | Final Rows |", "|---:|---:|---|---|---:|---:|---:|---:|---:|"])
    for item in sorted(summaries, key=lambda x: (x.get("wave", 0), x.get("sample_size", 0), x.get("language", ""))):
        lines.append(
            f"| {item.get('wave', '')} | {item.get('sample_size', '')} | `{item.get('language', '')}` | `{item.get('final_status', '')}` | "
            f"{item.get('candidate_commits', 0):,} | {item.get('import_events', 0):,} | {item.get('import_dependency_matches', 0):,} | "
            f"{item.get('registry_lookup_rows', 0):,} | {item.get('final_label_rows', 0):,} |"
        )
    lines.extend([
        "",
        "## Notes",
        "",
        "- Each language/sample run has independent logs under `logs/` and a `summary.json` in its run directory.",
        "- Partial runs are kept for debugging but are excluded from aggregate label counts unless `final_labels.csv` exists.",
        "- `unknown_deleted_or_unpublished_possible` remains weak current-registry evidence, not proof of historical hallucination.",
        "- Network and registry failures remain conservative `cannot_compare` labels.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    args.output_root.mkdir(parents=True, exist_ok=True)
    start = time.time()
    deadline = start + args.time_budget_hours * 3600
    with (args.output_root / "run_config.json").open("w", encoding="utf-8") as handle:
        json.dump(vars(args) | {"output_root": str(args.output_root)}, handle, ensure_ascii=False, indent=2)
    for wave, sample_size in enumerate(args.sample_sizes, 1):
        if time.time() >= deadline:
            break
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            futures = {
                executor.submit(pipeline_for_language, args, language, sample_size, wave): language
                for language in args.languages
                if time.time() < deadline
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    append_status(args.output_root, {
                        "timestamp": now(),
                        "wave": wave,
                        "language": futures[future],
                        "sample_size": sample_size,
                        "stage": "orchestrator",
                        "status": "exception",
                        "returncode": 1,
                        "message": repr(exc),
                    })
                write_interim_report(args.output_root, args)
        if not args.disable_wave_quality_gate:
            audit = run_wave_audit(args.output_root, wave, sample_size)
            decision = evaluate_wave_gate(args.output_root, wave, sample_size, audit)
            write_interim_report(args.output_root, args)
            if decision["status"] == "blocked":
                break
        if time.time() >= deadline:
            break
    write_final_report(args.output_root, args)


if __name__ == "__main__":
    main()

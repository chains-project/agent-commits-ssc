"""Rerun registry labeling for selected overnight experiment runs.

[IN]: completed `manifest_registry/` directories from run_refined_overnight.py.
[OUT]: refreshed registry/advisory/final-label files, updated summary.json, and
rerun_registry_status.csv under the selected output root.
[POS]: Repair step after registry-labeling code changes without refetching diffs
or child manifests.
[SYNC]: If outputs or invocation contract change, update scripts/OUTPUTS.md and
.claude/plans/2026-07-07-hallucination-full-pipeline.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PYTHON = Path(r"C:\ProgramData\Anaconda3\python.exe")
LANG_DIRS = {"C#": "CSharp", "C++": "Cpp", "F#": "FSharp"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--waves", nargs="+", required=True, help="Pairs like 3:100.")
    parser.add_argument("--languages", nargs="+", required=True)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--sleep-seconds", type=float, default=0.15)
    parser.add_argument("--retry-count", type=int, default=2)
    parser.add_argument("--retry-sleep-seconds", type=float, default=2.0)
    return parser.parse_args()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def lang_dir(language: str) -> str:
    return LANG_DIRS.get(language, language)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return list(csv.DictReader(line.replace("\x00", "") for line in handle))


def append_status(root: Path, row: dict[str, Any]) -> None:
    path = root / "rerun_registry_status.csv"
    fields = ["timestamp", "wave", "sample_size", "language", "status", "returncode", "duration_seconds", "run_dir", "log_path"]
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in fields})


def parse_wave(value: str) -> tuple[int, int]:
    wave, sep, sample = value.partition(":")
    if sep != ":":
        raise ValueError(f"invalid wave spec: {value}")
    return int(wave), int(sample)


def run_registry(args: argparse.Namespace, wave: int, sample_size: int, language: str) -> None:
    run_dir = args.output_root / f"wave{wave:02d}_n{sample_size}" / lang_dir(language)
    stage_dir = run_dir / "manifest_registry"
    log_path = args.output_root / "logs" / f"wave{wave:02d}_n{sample_size}" / lang_dir(language) / "stage3_registry_rerun.log"
    cmd = [
        str(PYTHON), "scripts/hallucination/registry_label.py",
        "--input-dir", str(stage_dir), "--output-dir", str(stage_dir),
        "--timeout", str(args.timeout), "--sleep-seconds", str(args.sleep_seconds),
        "--retry-count", str(args.retry_count),
        "--retry-sleep-seconds", str(args.retry_sleep_seconds),
    ]
    start = time.time()
    result = execute(cmd, log_path)
    duration = round(time.time() - start, 2)
    update_summary(run_dir, wave, sample_size, language, result.returncode, duration)
    append_status(args.output_root, {
        "timestamp": now(), "wave": wave, "sample_size": sample_size,
        "language": language, "status": "ok" if result.returncode == 0 else "failed",
        "returncode": result.returncode, "duration_seconds": duration,
        "run_dir": str(run_dir), "log_path": str(log_path),
    })


def execute(cmd: list[str], log_path: Path) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(cmd, cwd=Path.cwd(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log_path.write_text("command=" + " ".join(cmd) + "\n\n" + (result.stdout or ""), encoding="utf-8")
    return result


def update_summary(run_dir: Path, wave: int, sample_size: int, language: str, returncode: int, duration: float) -> None:
    summary_path = run_dir / "summary.json"
    summary = {}
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8", errors="replace"))
    summary.update({"language": language, "sample_size": sample_size, "wave": wave, "run_dir": str(run_dir)})
    summary["stage3_registry_label"] = {"status": "ok" if returncode == 0 else "failed", "returncode": returncode, "duration_seconds": duration}
    summary.update(collect_counts(run_dir / "stage1", run_dir / "manifest_registry"))
    summary["final_status"] = "ok" if returncode == 0 else "failed_at_stage3_registry_label"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_counts(stage1_dir: Path, stage2_dir: Path) -> dict[str, Any]:
    final_rows = read_csv(stage2_dir / "final_labels.csv")
    registry_rows = read_csv(stage2_dir / "registry_lookup_results.csv")
    match_rows = read_csv(stage2_dir / "import_dependency_matches.csv")
    labels = Counter(row.get("final_label", "") for row in final_rows)
    reasons = Counter(row.get("cannot_compare_reason", "") for row in final_rows if row.get("cannot_compare_reason"))
    buckets = Counter(row.get("registry_failure_bucket", "") for row in registry_rows)
    return {
        "candidate_commits": len(read_csv(stage1_dir / "candidate_commits.csv")),
        "import_events": len(read_csv(stage1_dir / "import_events.csv")),
        "import_dependency_matches": len(match_rows),
        "registry_lookup_rows": len(registry_rows),
        "final_label_rows": len(final_rows),
        "final_labels": dict(labels),
        "cannot_compare_reasons": dict(reasons),
        "registry_failure_buckets": dict(buckets),
    }


def main() -> None:
    args = parse_args()
    for wave_spec in args.waves:
        wave, sample_size = parse_wave(wave_spec)
        for language in args.languages:
            run_registry(args, wave, sample_size, language)


if __name__ == "__main__":
    main()

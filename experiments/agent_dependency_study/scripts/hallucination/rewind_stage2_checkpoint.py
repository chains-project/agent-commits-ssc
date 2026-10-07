"""Safely rewind ordered Stage2 append CSVs to a candidate boundary.

[IN]: candidate_commits.csv, Stage2 fetch CSVs, and stage2_checkpoint.json.
[OUT]: dry-run report or truncated CSVs/checkpoint plus exact suffix backups.
[POS]: Recovery utility for retryable environmental failures already persisted
past a durable candidate boundary.
[SYNC]: If Stage2 schemas or checkpoint keys change, update manifest_enrich,
tests, scripts/CLAUDE.md, and scripts/OUTPUTS.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from stage2_storage import CsvSpec, atomic_write_json, iter_csv_rows, read_json


KEY_FIELDS = ("repo", "sha")
REPORT_NAME = "rewind_report.json"
CHECKPOINT_NAME = "stage2_checkpoint.json"


def raise_csv_field_limit() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


raise_csv_field_limit()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-csv", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--target-processed", type=int)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore", action="store_true")
    return parser.parse_args()


def candidate_positions(path: Path, limit: int) -> dict[tuple[str, str], int]:
    positions: dict[tuple[str, str], int] = {}
    for index, row in enumerate(iter_csv_rows(path)):
        if index >= limit:
            break
        key = tuple(row.get(field, "") for field in KEY_FIELDS)
        if not all(key) or key in positions:
            raise ValueError(f"invalid or duplicate candidate key at row {index + 2}: {key}")
        positions[key] = index + 1
    if len(positions) != limit:
        raise ValueError(f"candidate CSV has {len(positions)} rows before limit {limit}")
    return positions


class BinaryCsvLines:
    def __init__(self, handle: object) -> None:
        self.handle = handle
        self.position = 0
        self.first = True

    def __iter__(self) -> "BinaryCsvLines":
        return self

    def __next__(self) -> str:
        raw = self.handle.readline()
        if not raw:
            raise StopIteration
        self.position = self.handle.tell()
        encoding = "utf-8-sig" if self.first else "utf-8"
        self.first = False
        return raw.decode(encoding)


def validate_header(values: Sequence[str], fields: Sequence[str], path: Path) -> None:
    if values != list(fields):
        raise ValueError(f"unexpected CSV schema: {path}")


def scan_rewind_point(
    path: Path, spec: CsvSpec, positions: Mapping[tuple[str, str], int], target: int,
) -> dict[str, int]:
    with path.open("rb") as handle:
        lines = BinaryCsvLines(handle)
        reader = csv.reader(lines)
        validate_header(next(reader), spec.fields, path)
        cut_bytes, retained_rows, previous = lines.position, 0, 0
        indexes = tuple(spec.fields.index(field) for field in KEY_FIELDS)
        for values in reader:
            if len(values) != len(spec.fields):
                raise ValueError(f"malformed CSV record in {path}")
            key = tuple(values[index] for index in indexes)
            position = positions.get(key)
            if position is None or position < previous:
                raise ValueError(f"unordered or unknown candidate key in {path}: {key}")
            if position > target:
                break
            retained_rows += 1
            cut_bytes, previous = lines.position, position
    return {
        "retained_rows": retained_rows,
        "cut_bytes": cut_bytes,
        "original_bytes": path.stat().st_size,
    }


def build_report(
    candidate_csv: Path, output_dir: Path, specs: Mapping[str, CsvSpec], target: int,
) -> dict[str, object]:
    checkpoint_path = output_dir / CHECKPOINT_NAME
    checkpoint = read_json(checkpoint_path)
    current = int(checkpoint.get("processed_candidate_commits", 0) or 0)
    if target < 0 or target >= current:
        raise ValueError(f"target must be between 0 and {current - 1}: {target}")
    positions = candidate_positions(candidate_csv, current)
    files = {}
    for name, spec in specs.items():
        details = scan_rewind_point(
            output_dir / spec.filename, spec, positions, target
        )
        details["filename"] = spec.filename
        files[name] = details
    return {
        "status": "dry_run",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_csv": str(candidate_csv),
        "output_dir": str(output_dir),
        "original_processed_candidate_commits": current,
        "target_processed_candidate_commits": target,
        "files": files,
    }


def copy_tail(source: Path, target: Path, offset: int) -> None:
    with source.open("rb") as reader, target.open("wb") as writer:
        reader.seek(offset)
        shutil.copyfileobj(reader, writer, length=8 * 1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())


def prepare_backup(
    output_dir: Path, backup_dir: Path, specs: Mapping[str, CsvSpec],
    report: dict[str, object],
) -> None:
    if backup_dir.exists():
        raise FileExistsError(f"backup directory already exists: {backup_dir}")
    backup_dir.mkdir(parents=True)
    shutil.copy2(output_dir / CHECKPOINT_NAME, backup_dir / CHECKPOINT_NAME)
    files = report["files"]
    for name, spec in specs.items():
        details = files[name]
        copy_tail(
            output_dir / spec.filename,
            backup_dir / f"{spec.filename}.tail",
            int(details["cut_bytes"]),
        )
    atomic_write_json(backup_dir / REPORT_NAME, report)


def truncate_file(path: Path, size: int) -> None:
    with path.open("r+b") as handle:
        handle.truncate(size)
        handle.flush()
        os.fsync(handle.fileno())


def rewound_checkpoint(
    output_dir: Path, specs: Mapping[str, CsvSpec], report: dict[str, object],
) -> dict[str, object]:
    state = read_json(output_dir / CHECKPOINT_NAME)
    files = report["files"]
    state["processed_candidate_commits"] = report["target_processed_candidate_commits"]
    state["fetch_complete"] = False
    state["complete"] = False
    state["generated_utc"] = datetime.now(timezone.utc).isoformat()
    state["csv_bytes"] = {}
    for name, spec in specs.items():
        state[spec.count_key] = files[name]["retained_rows"]
        state["csv_bytes"][name] = files[name]["cut_bytes"]
    return state


def apply_rewind(
    output_dir: Path, backup_dir: Path, specs: Mapping[str, CsvSpec],
    report: dict[str, object],
) -> None:
    prepare_backup(output_dir, backup_dir, specs, report)
    try:
        for name, spec in specs.items():
            truncate_file(
                output_dir / spec.filename,
                int(report["files"][name]["cut_bytes"]),
            )
        atomic_write_json(
            output_dir / CHECKPOINT_NAME,
            rewound_checkpoint(output_dir, specs, report),
        )
    except BaseException:
        restore_rewind(output_dir, backup_dir)
        raise


def rewind_stage2(
    candidate_csv: Path, output_dir: Path, specs: Mapping[str, CsvSpec],
    target: int, backup_dir: Path, *, apply: bool,
) -> dict[str, object]:
    report = build_report(candidate_csv, output_dir, specs, target)
    if not apply:
        return report
    apply_rewind(output_dir, backup_dir, specs, report)
    report["status"] = "applied"
    report["applied_utc"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(backup_dir / REPORT_NAME, report)
    return report


def append_tail(path: Path, tail: Path, cut_bytes: int, original_bytes: int) -> None:
    truncate_file(path, cut_bytes)
    with path.open("ab") as writer, tail.open("rb") as reader:
        shutil.copyfileobj(reader, writer, length=8 * 1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    if path.stat().st_size != original_bytes:
        raise ValueError(f"restored size mismatch: {path}")


def restore_rewind(output_dir: Path, backup_dir: Path) -> None:
    report = read_json(backup_dir / REPORT_NAME)
    for details in report["files"].values():
        filename = Path(details.get("filename", "")).name
        if not filename:
            continue
        append_tail(
            output_dir / filename,
            backup_dir / f"{filename}.tail",
            int(details["cut_bytes"]),
            int(details["original_bytes"]),
        )
    shutil.copy2(backup_dir / CHECKPOINT_NAME, output_dir / CHECKPOINT_NAME)


def main() -> None:
    args = parse_args()
    if args.restore:
        restore_rewind(args.output_dir, args.backup_dir)
        print(json.dumps({"status": "restored"}, indent=2))
        return
    if args.candidate_csv is None or args.target_processed is None:
        raise SystemExit("--candidate-csv and --target-processed are required")
    from manifest_enrich import stage2_fetch_specs

    specs = stage2_fetch_specs()
    report = build_report(
        args.candidate_csv, args.output_dir, specs, args.target_processed
    )
    if args.apply:
        apply_rewind(args.output_dir, args.backup_dir, specs, report)
        report["status"] = "applied"
        atomic_write_json(args.backup_dir / REPORT_NAME, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

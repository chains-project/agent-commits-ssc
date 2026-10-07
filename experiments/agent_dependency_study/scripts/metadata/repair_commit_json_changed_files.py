"""Repair changed_files.csv rows from an already saved commit JSON document.

[IN]: One commit JSON emitted by fetch-commit-json-diffs-with-languages.py and
the corpus-level changed_files.csv.
[OUT]: An idempotent append of the commit's changed-file rows plus an optional
JSON audit report.
[POS]: Narrow recovery tool for a commit indexed as `ok` before its changed-file
batch was durably appended.
[SYNC]: If the changed-files schema changes, update the collector, tests,
scripts/CLAUDE.md, and scripts/OUTPUTS.md together.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any


CHANGED_FILE_FIELDS = [
    "repo_language",
    "commit_primary_language",
    "repo",
    "sha",
    "filename",
    "previous_filename",
    "status",
    "file_language",
    "file_language_source",
    "is_supply_chain_file",
    "supply_chain_match_type",
    "dependency_ecosystem",
    "package_manager",
    "supply_chain_role",
    "additions",
    "deletions",
    "changes",
    "has_patch",
    "raw_url",
    "blob_url",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-path", type=Path, required=True)
    parser.add_argument("--changed-files-csv", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def changed_file_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    shared = {
        "repo_language": data.get("repo_language", ""),
        "commit_primary_language": data.get("commit_primary_language", ""),
        "repo": data.get("repo", ""),
        "sha": data.get("sha", ""),
    }
    return [changed_file_row(shared, file_row) for file_row in data.get("files", [])]


def changed_file_row(
    shared: dict[str, Any], file_row: dict[str, Any]
) -> dict[str, Any]:
    return {
        **shared,
        "filename": file_row.get("filename", ""),
        "previous_filename": file_row.get("previous_filename", ""),
        "status": file_row.get("status", ""),
        "file_language": file_row.get("file_language", ""),
        "file_language_source": file_row.get("file_language_source", ""),
        "is_supply_chain_file": file_row.get("is_supply_chain_file", "false"),
        "supply_chain_match_type": file_row.get("supply_chain_match_type", ""),
        "dependency_ecosystem": file_row.get("dependency_ecosystem", ""),
        "package_manager": file_row.get("package_manager", ""),
        "supply_chain_role": file_row.get("supply_chain_role", ""),
        "additions": file_row.get("additions", ""),
        "deletions": file_row.get("deletions", ""),
        "changes": file_row.get("changes", ""),
        "has_patch": bool(file_row.get("patch")),
        "raw_url": file_row.get("raw_url", ""),
        "blob_url": file_row.get("blob_url", ""),
    }


def count_existing_sha(path: Path, sha: str) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(line.replace("\x00", "") for line in handle):
            count += row.get("sha", "") == sha
    return count


def validate_header(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle), [])
    if header != CHANGED_FILE_FIELDS:
        raise ValueError(f"unexpected changed-files schema: {path}")


def append_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    validate_header(path)
    existed = path.exists()
    original_size = path.stat().st_size if existed else 0
    try:
        append_rows_unchecked(path, rows, write_header=not existed or original_size == 0)
    except BaseException:
        rollback_append(path, existed, original_size)
        raise


def append_rows_unchecked(
    path: Path, rows: list[dict[str, Any]], *, write_header: bool
) -> None:
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CHANGED_FILE_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def rollback_append(path: Path, existed: bool, original_size: int) -> None:
    if not existed:
        path.unlink(missing_ok=True)
        return
    with path.open("r+b") as handle:
        handle.truncate(original_size)
        handle.flush()
        os.fsync(handle.fileno())


def repair_changed_files(
    json_path: Path, changed_files_csv: Path, *, apply: bool
) -> dict[str, Any]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    rows = changed_file_rows(data)
    sha = str(data.get("sha", ""))
    if not sha or not data.get("repo") or not rows:
        raise ValueError("commit JSON must contain repo, sha, and non-empty files")
    existed = changed_files_csv.exists()
    original_size = changed_files_csv.stat().st_size if existed else 0
    existing = count_existing_sha(changed_files_csv, sha)
    result = repair_result(data, len(rows), existing, apply)
    result.update({
        "json_path": str(json_path),
        "changed_files_csv": str(changed_files_csv),
        "original_csv_bytes": original_size,
        "final_csv_bytes": original_size,
        "verified_rows": existing,
    })
    if result["status"] == "ready_to_repair" and apply:
        append_and_verify(
            changed_files_csv, rows, sha, existed, original_size
        )
        result["status"] = "repaired"
        result["appended_rows"] = len(rows)
        result["verified_rows"] = len(rows)
        result["final_csv_bytes"] = changed_files_csv.stat().st_size
    return result


def append_and_verify(
    path: Path, rows: list[dict[str, Any]], sha: str,
    existed: bool, original_size: int,
) -> None:
    try:
        append_rows(path, rows)
        verified = count_existing_sha(path, sha)
        if verified != len(rows):
            raise ValueError(
                f"post-append verification failed: sha={sha} "
                f"expected={len(rows)} actual={verified}"
            )
    except BaseException:
        rollback_append(path, existed, original_size)
        raise


def repair_result(
    data: dict[str, Any], expected: int, existing: int, apply: bool
) -> dict[str, Any]:
    status = "ready_to_repair"
    if existing == expected:
        status = "already_complete"
    elif existing:
        status = "partial_existing_refused"
    elif not apply:
        status = "dry_run_missing"
    return {
        "repo": data.get("repo", ""),
        "sha": data.get("sha", ""),
        "expected_rows": expected,
        "existing_rows": existing,
        "appended_rows": 0,
        "status": status,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def main() -> None:
    args = parse_args()
    report = repair_changed_files(
        args.json_path, args.changed_files_csv, apply=args.apply
    )
    if args.report:
        write_report(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] == "partial_existing_refused":
        raise SystemExit(2)


if __name__ == "__main__":
    main()

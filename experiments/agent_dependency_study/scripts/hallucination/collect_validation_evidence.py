"""Collect offline diff and child-cache evidence for validation cases.

[IN]: case_inventory.csv, local compact commit JSON/patch files referenced by
the inventory, and the existing Stage2 child manifest cache.
[OUT]: local_evidence.csv, local_evidence_manifest.json,
local_evidence_invariants.json, and case_evidence/<case_id>.json.
[POS]: Offline Phase 2 history-candidate evidence collector. It produces review hints, never a
confirmed hallucination or false-positive adjudication.
[SYNC]: If evidence fields, automatic hints, or invariants change, update
scripts/OUTPUTS.md and the active hallucination validation plan.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from build_validation_cases import (
    HISTORY_VALIDATION_LABELS,
    csv_rows,
    fingerprint_file,
)
from cache_storage import read_cache_text
from manifest_enrich import DATA_ROOT, manifest_cache_target


DEFAULT_VALIDATION_ROOT = Path("data_products/rq2_hallucinated_validation_20260721")
DEFAULT_INVENTORY = DEFAULT_VALIDATION_ROOT / "case_inventory.csv"
DEFAULT_CACHE = DATA_ROOT / "cache" / "rq2_hallucinated"
STRONG_LABEL = "version_published_after_author_date"
SCHEMA_VERSION = 1
DIFF_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$")

EVIDENCE_FIELDS = (
    "case_id",
    "language",
    "agent",
    "repo",
    "sha",
    "ecosystem",
    "package_name",
    "version",
    "final_label",
    "author_date",
    "registry_publish_time",
    "publish_after_author_seconds",
    "patch_exists",
    "json_exists",
    "dependency_files_total",
    "dependency_files_changed_delta",
    "dependency_patch_sections",
    "child_cache_files_available",
    "dependency_file_changed_inventory",
    "dependency_file_changed_delta",
    "inventory_delta_agree",
    "package_added_line_count",
    "package_deleted_line_count",
    "version_added_line_count",
    "version_deleted_line_count",
    "local_change_class",
    "requires_parent_metadata",
    "requires_committer_time",
    "local_evidence_status",
    "evidence_json_path",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_VALIDATION_ROOT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--labels", nargs="+", default=sorted(HISTORY_VALIDATION_LABELS))
    return parser.parse_args()


def split_patch_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        match = DIFF_HEADER.match(line)
        if match:
            current = match.group(2)
            sections.setdefault(current, []).append(line)
            continue
        if current:
            sections[current].append(line)
    return {path: "\n".join(lines) for path, lines in sections.items()}


def changed_paths(payload: dict[str, Any]) -> set[str]:
    paths = set()
    for row in payload.get("files", []) or []:
        for field in ("filename", "previous_filename"):
            value = str(row.get(field, "") or "").strip()
            if value:
                paths.add(value)
    return paths


def patch_change_lines(section: str) -> tuple[list[str], list[str]]:
    added = []
    deleted = []
    for line in section.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            deleted.append(line[1:])
    return added, deleted


def matching_lines(lines: list[str], needle: str) -> list[str]:
    lowered = needle.lower()
    return [line for line in lines if lowered and lowered in line.lower()]


def content_fingerprint(text: str, actual_path: Path | None) -> dict[str, Any]:
    data = text.encode("utf-8")
    return {
        "cache_path": str(actual_path.resolve()) if actual_path else "",
        "content_bytes": len(data),
        "content_sha256": hashlib.sha256(data).hexdigest(),
    }


def child_cache_evidence(
    cache_dir: Path, repo: str, sha: str, path: str
) -> dict[str, Any]:
    target = manifest_cache_target(cache_dir, repo, sha, path)
    text, actual_path = read_cache_text(target)
    if text is None:
        return {"available": False, "logical_path": str(target), "cache_path": ""}
    evidence = {"available": True, "logical_path": str(target)}
    evidence.update(content_fingerprint(text, actual_path))
    return evidence


def dependency_file_evidence(
    case: dict[str, str],
    path: str,
    sections: dict[str, str],
    delta_paths: set[str],
    cache_dir: Path,
) -> dict[str, Any]:
    section = sections.get(path, "")
    added, deleted = patch_change_lines(section)
    package = case["package_name"]
    version = case["version"]
    return {
        "path": path,
        "changed_in_delta": path in delta_paths,
        "patch_section_available": bool(section),
        "matching_added_lines": matching_lines(added, package),
        "matching_deleted_lines": matching_lines(deleted, package),
        "version_added_lines": matching_lines(added, version),
        "version_deleted_lines": matching_lines(deleted, version),
        "child_cache": child_cache_evidence(
            cache_dir, case["repo"], case["sha"], path
        ),
    }


def load_json_file(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig", errors="replace")), "ok"
    except (OSError, json.JSONDecodeError):
        return {}, "invalid"


def load_patch_file(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "", "missing"
    try:
        return path.read_text(encoding="utf-8", errors="replace"), "ok"
    except OSError:
        return "", "invalid"


def json_list(value: str) -> list[str]:
    if not value:
        return []
    parsed = json.loads(value)
    return [str(item) for item in parsed if str(item)]


def count_evidence(files: list[dict[str, Any]], key: str) -> int:
    return sum(len(item[key]) for item in files)


def classify_local_change(
    changed: bool, files: list[dict[str, Any]]
) -> str:
    if not changed:
        return "dependency_file_unchanged"
    if not any(item["patch_section_available"] for item in files):
        return "patch_unavailable"
    package_added = count_evidence(files, "matching_added_lines")
    package_deleted = count_evidence(files, "matching_deleted_lines")
    version_added = count_evidence(files, "version_added_lines")
    version_deleted = count_evidence(files, "version_deleted_lines")
    if package_added and package_deleted:
        return "package_or_version_changed"
    if package_added:
        return "package_added_or_lock_entry"
    if version_added and version_deleted:
        return "version_changed_without_package_key"
    if version_added:
        return "version_added_in_diff"
    return "changed_no_exact_match"


def parse_iso(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def publish_gap_seconds(case: dict[str, str]) -> tuple[str, str]:
    times = json_list(case.get("registry_publish_times_json", ""))
    if len(times) != 1 or not case.get("author_date"):
        return "", "missing_or_conflicting_timestamp"
    try:
        gap = parse_iso(times[0]) - parse_iso(case["author_date"])
    except ValueError:
        return "", "invalid_timestamp"
    return str(int(gap.total_seconds())), "ok"


def source_fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "status": "missing"}
    result = fingerprint_file(path)
    result["status"] = "ok"
    return result


def automatic_flags(
    case: dict[str, str],
    files: list[dict[str, Any]],
    changed_delta: bool,
) -> dict[str, Any]:
    return {
        "dependency_file_changed_inventory": case[
            "dependency_file_changed_any"
        ].lower()
        == "true",
        "dependency_file_changed_delta": changed_delta,
        "package_added_in_diff": count_evidence(files, "matching_added_lines") > 0,
        "package_deleted_in_diff": count_evidence(files, "matching_deleted_lines") > 0,
        "version_added_in_diff": count_evidence(files, "version_added_lines") > 0,
        "version_deleted_in_diff": count_evidence(files, "version_deleted_lines") > 0,
        "requires_parent_metadata": True,
        "requires_committer_time": True,
    }


def case_identity(case: dict[str, str]) -> dict[str, str]:
    fields = (
        "case_id", "language", "agent", "repo", "sha", "ecosystem",
        "package_name", "version", "final_label", "author_date",
    )
    return {key: case.get(key, "") for key in fields}


def assemble_bundle(
    case: dict[str, str],
    files: list[dict[str, Any]],
    delta_paths: set[str],
    patch_path: Path,
    patch_status: str,
    json_path: Path,
    json_status: str,
) -> dict[str, Any]:
    changed_delta = any(item["changed_in_delta"] for item in files)
    gap, timestamp_status = publish_gap_seconds(case)
    return {
        "schema_version": SCHEMA_VERSION,
        "case": case_identity(case),
        "registry_publish_times": json_list(case.get("registry_publish_times_json", "")),
        "publish_after_author_seconds": gap,
        "timestamp_status": timestamp_status,
        "patch_source": source_fingerprint(patch_path),
        "patch_status": patch_status,
        "json_source": source_fingerprint(json_path),
        "json_status": json_status,
        "delta_changed_paths": sorted(delta_paths),
        "dependency_files": files,
        "automatic_flags": automatic_flags(case, files, changed_delta),
        "local_change_class": classify_local_change(changed_delta, files),
        "manual_adjudication": None,
    }


def build_case_bundle(case: dict[str, str], cache_dir: Path) -> dict[str, Any]:
    patch_path = Path(case["patch_path"])
    json_path = Path(case["json_path"])
    patch_text, patch_status = load_patch_file(patch_path)
    payload, json_status = load_json_file(json_path)
    sections = split_patch_sections(patch_text)
    delta_paths = changed_paths(payload)
    paths = json_list(case.get("dep_file_paths_json", ""))
    files = [
        dependency_file_evidence(case, path, sections, delta_paths, cache_dir)
        for path in paths
    ]
    return assemble_bundle(
        case, files, delta_paths, patch_path, patch_status, json_path, json_status
    )


def evidence_count_summary(files: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "dependency_files_total": len(files),
        "dependency_files_changed_delta": sum(item["changed_in_delta"] for item in files),
        "dependency_patch_sections": sum(item["patch_section_available"] for item in files),
        "child_cache_files_available": sum(item["child_cache"]["available"] for item in files),
        "package_added_line_count": count_evidence(files, "matching_added_lines"),
        "package_deleted_line_count": count_evidence(files, "matching_deleted_lines"),
        "version_added_line_count": count_evidence(files, "version_added_lines"),
        "version_deleted_line_count": count_evidence(files, "version_deleted_lines"),
    }


def timestamp_summary(bundle: dict[str, Any]) -> dict[str, str]:
    times = bundle["registry_publish_times"]
    return {
        "registry_publish_time": times[0] if len(times) == 1 else "",
        "publish_after_author_seconds": bundle["publish_after_author_seconds"],
    }


def bundle_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    case = bundle["case"]
    files = bundle["dependency_files"]
    flags = bundle["automatic_flags"]
    return {
        **case,
        **timestamp_summary(bundle),
        **evidence_count_summary(files),
        "patch_exists": str(bundle["patch_status"] == "ok").lower(),
        "json_exists": str(bundle["json_status"] == "ok").lower(),
        "dependency_file_changed_inventory": str(flags["dependency_file_changed_inventory"]).lower(),
        "dependency_file_changed_delta": str(flags["dependency_file_changed_delta"]).lower(),
        "inventory_delta_agree": str(
            flags["dependency_file_changed_inventory"]
            == flags["dependency_file_changed_delta"]
        ).lower(),
        "local_change_class": bundle["local_change_class"],
        "requires_parent_metadata": "true",
        "requires_committer_time": "true",
        "local_evidence_status": local_evidence_status(bundle),
        "evidence_json_path": f"case_evidence/{case['case_id']}.json",
    }


def local_evidence_status(bundle: dict[str, Any]) -> str:
    if bundle["patch_status"] != "ok" or bundle["json_status"] != "ok":
        return "missing_local_source"
    if bundle["timestamp_status"] != "ok":
        return "timestamp_unavailable"
    files = bundle["dependency_files"]
    if not files or not all(item["child_cache"]["available"] for item in files):
        return "child_cache_incomplete"
    return "ready_for_parent_and_manual_review"


def build_local_evidence(
    inventory_path: Path,
    cache_dir: Path,
    labels: Iterable[str],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    selected_labels = set(labels)
    bundles = {}
    for case in csv_rows(inventory_path):
        if case.get("final_label") not in selected_labels:
            continue
        bundle = build_case_bundle(case, cache_dir)
        bundles[case["case_id"]] = bundle
    rows = [bundle_summary(bundle) for bundle in bundles.values()]
    rows.sort(key=lambda row: row["case_id"])
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "inventory": fingerprint_file(inventory_path),
        "cache_dir": str(cache_dir.resolve()),
        "selected_labels": sorted(selected_labels),
        "selected_cases": len(rows),
    }
    return rows, bundles, manifest


def evidence_invariant_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    ids = [row["case_id"] for row in rows]
    return {
        "duplicate_case_ids": len(ids) - len(set(ids)),
        "missing_patch_cases": sum(row["patch_exists"] != "true" for row in rows),
        "missing_json_cases": sum(row["json_exists"] != "true" for row in rows),
        "missing_dependency_path_cases": sum(
            int(row["dependency_files_total"]) == 0 for row in rows
        ),
        "incomplete_child_cache_cases": sum(
            int(row["child_cache_files_available"]) < int(row["dependency_files_total"])
            for row in rows
        ),
        "inventory_delta_mismatch_cases": sum(
            row["inventory_delta_agree"] != "true" for row in rows
        ),
        "timestamp_unavailable_cases": sum(
            row["publish_after_author_seconds"] == "" for row in rows
        ),
        "nonpositive_strong_gap_cases": sum(
            row["final_label"] in HISTORY_VALIDATION_LABELS
            and row["publish_after_author_seconds"] != ""
            and int(row["publish_after_author_seconds"]) <= 0
            for row in rows
        ),
    }


def validate_local_evidence(
    rows: list[dict[str, Any]],
    bundles: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    report = {
        "schema_version": SCHEMA_VERSION,
        "checked_cases": len(rows),
        **evidence_invariant_counts(rows),
        "bundle_count_mismatch": int(len(bundles) != len(rows)),
        "selected_count_mismatch": int(manifest["selected_cases"] != len(rows)),
    }
    ignored = {"schema_version", "checked_cases"}
    report["status"] = (
        "pass" if sum(value for key, value in report.items() if key not in ignored) == 0
        else "fail"
    )
    return report


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=EVIDENCE_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_local_evidence_outputs(
    output_dir: Path,
    rows: list[dict[str, Any]],
    bundles: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
    invariants: dict[str, Any],
) -> None:
    for case_id, bundle in bundles.items():
        atomic_write_json(output_dir / "case_evidence" / f"{case_id}.json", bundle)
    evidence_path = output_dir / "local_evidence.csv"
    atomic_write_csv(evidence_path, rows)
    manifest["output"] = fingerprint_file(evidence_path)
    manifest["output"]["rows"] = len(rows)
    atomic_write_json(output_dir / "local_evidence_manifest.json", manifest)
    atomic_write_json(output_dir / "local_evidence_invariants.json", invariants)


def main() -> None:
    args = parse_args()
    rows, bundles, manifest = build_local_evidence(
        args.inventory, args.cache_dir, args.labels
    )
    invariants = validate_local_evidence(rows, bundles, manifest)
    write_local_evidence_outputs(
        args.output_dir, rows, bundles, manifest, invariants
    )
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "cases": len(rows),
                "invariants": invariants["status"],
            },
            ensure_ascii=False,
        )
    )
    if invariants["status"] != "pass":
        raise SystemExit(2)


if __name__ == "__main__":
    main()

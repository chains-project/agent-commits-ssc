"""Build a deduplicated, auditable inventory of hallucination candidates.

[IN]: Per-language manifest_registry/final_labels.csv and
stage1/candidate_commits.csv from an unlimited hallucination pipeline run.
[OUT]: case_inventory.csv, case_inventory_manifest.json, and
case_inventory_invariants.json under a separate validation data product.
[POS]: Offline, read-only bridge from row-level Stage3 labels to manual-review
case units; it does not change Stage1, Stage2, or Stage3 labels.
[SYNC]: If case keys, output fields, or validation invariants change, update
scripts/OUTPUTS.md and the active hallucination validation plan.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


DEFAULT_INPUT_ROOT = Path(
    "data_products/rq2_hallucinated_unlimited_overnight_20260710"
)
DEFAULT_OUTPUT_DIR = Path("data_products/rq2_hallucinated_validation_20260721")
DEFAULT_LANGUAGES = ("TypeScript", "JavaScript")
HISTORY_VALIDATION_LABELS = frozenset(
    {
        "version_published_after_author_date",
        "version_published_after_author_date_candidate",
    }
)
TARGET_LABELS = HISTORY_VALIDATION_LABELS | {
    "unknown_deleted_or_unpublished_possible",
}
SCHEMA_VERSION = 1

SET_FIELD_SOURCES = {
    "source_files": "source_file",
    "import_raws": "import_raw",
    "import_roots": "import_root",
    "dep_file_paths": "dep_file_path",
    "declared_packages": "declared_package",
    "version_specs": "version_spec",
    "resolved_versions": "resolved_version",
    "resolution_sources": "resolution_source",
    "mapping_statuses": "mapping_status",
    "declared_dependency_matches": "declared_dependency_match",
    "dependency_groups": "dependency_group",
    "registry_publish_times": "registry_publish_time",
    "registry_sources": "registry_registry_source",
    "registry_statuses": "registry_registry_status",
    "registry_failure_buckets": "registry_registry_failure_bucket",
    "evidence_strengths": "evidence_strength",
}

CANDIDATE_FIELD_SOURCES = {
    "commit_primary_languages": "commit_primary_language",
    "patch_paths": "patch_path",
    "json_paths": "json_path",
    "manifest_touched_values": "manifest_touched_in_diff",
    "lockfile_touched_values": "lockfile_touched_in_diff",
    "supply_chain_touched_values": "touches_supply_chain",
}

INVENTORY_FIELDS = (
    "case_id",
    "language",
    "languages_json",
    "repo",
    "sha",
    "ecosystem",
    "package_name",
    "version",
    "final_label",
    "final_labels_json",
    "agent",
    "agents_json",
    "author_date",
    "author_dates_json",
    "row_count",
    "dependency_file_changed_any",
    "dependency_file_changed_all",
    "patch_path",
    "patch_paths_json",
    "json_path",
    "json_paths_json",
    "commit_primary_language",
    "commit_primary_languages_json",
    "manifest_touched_in_diff",
    "lockfile_touched_in_diff",
    "touches_supply_chain",
    "source_files_json",
    "import_raws_json",
    "import_roots_json",
    "dep_file_paths_json",
    "declared_packages_json",
    "version_specs_json",
    "resolved_versions_json",
    "resolution_sources_json",
    "mapping_statuses_json",
    "declared_dependency_matches_json",
    "dependency_groups_json",
    "registry_publish_times_json",
    "registry_sources_json",
    "registry_statuses_json",
    "registry_failure_buckets_json",
    "evidence_strengths_json",
)


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
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--languages", nargs="+", default=list(DEFAULT_LANGUAGES))
    parser.add_argument("--labels", nargs="+", default=sorted(TARGET_LABELS))
    return parser.parse_args()


def clean_csv_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for line in handle:
            yield line.replace("\x00", "")


def csv_rows(path: Path) -> Iterator[dict[str, str]]:
    yield from csv.DictReader(clean_csv_lines(path))


def normalize_package(ecosystem: str, package: str) -> str:
    value = package.strip()
    if ecosystem.strip().lower() == "npm":
        return value.lower()
    return value


def normalized_case_parts(
    repo: str, sha: str, ecosystem: str, package: str, version: str
) -> tuple[str, str, str, str, str]:
    normalized_ecosystem = ecosystem.strip().lower()
    return (
        repo.strip().lower(),
        sha.strip().lower(),
        normalized_ecosystem,
        normalize_package(normalized_ecosystem, package),
        version.strip(),
    )


def case_id_for(
    repo: str, sha: str, ecosystem: str, package: str, version: str
) -> str:
    parts = normalized_case_parts(repo, sha, ecosystem, package, version)
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"hdep_{digest[:20]}"


def package_from_row(row: dict[str, str]) -> str:
    return (
        row.get("query_package")
        or row.get("registry_package_name")
        or row.get("declared_package")
        or row.get("package_candidate")
        or ""
    )


def version_from_row(row: dict[str, str]) -> str:
    return (
        row.get("query_version")
        or row.get("registry_version")
        or row.get("resolved_version")
        or ""
    )


def case_key_from_row(row: dict[str, str]) -> tuple[str, ...] | None:
    values = (
        row.get("repo", ""),
        row.get("sha", ""),
        row.get("ecosystem", ""),
        package_from_row(row),
        version_from_row(row),
    )
    if not all(value.strip() for value in values):
        return None
    return normalized_case_parts(*values)


def new_case(key: tuple[str, ...], language: str) -> dict[str, Any]:
    repo, sha, ecosystem, package, version = key
    case = {
        "repo": repo,
        "sha": sha,
        "ecosystem": ecosystem,
        "package_name": package,
        "version": version,
        "row_count": 0,
        "languages": {language},
        "final_labels": set(),
        "agents": set(),
        "author_dates": set(),
        "dependency_changed_values": set(),
        "candidate_found": False,
    }
    for name in (*SET_FIELD_SOURCES, *CANDIDATE_FIELD_SOURCES):
        case[name] = set()
    return case


def add_nonempty(target: set[str], value: str | None) -> None:
    if value is not None and value.strip():
        target.add(value.strip())


def add_label_row(case: dict[str, Any], row: dict[str, str], language: str) -> None:
    case["row_count"] += 1
    case["languages"].add(language)
    add_nonempty(case["final_labels"], row.get("final_label"))
    add_nonempty(case["agents"], row.get("agent"))
    add_nonempty(case["author_dates"], row.get("author_date"))
    changed = (row.get("dependency_file_changed_in_diff") or "").strip().lower()
    add_nonempty(case["dependency_changed_values"], changed)
    for name, source in SET_FIELD_SOURCES.items():
        add_nonempty(case[name], row.get(source))


def new_scan_stats() -> dict[str, Any]:
    return {
        "scanned_rows": 0,
        "target_rows": 0,
        "invalid_target_rows": 0,
        "commits": set(),
        "cases": set(),
        "package_versions": set(),
        "label_rows": Counter(),
        "label_commits": defaultdict(set),
        "label_cases": defaultdict(set),
    }


def record_scan_stats(
    stats: dict[str, Any], row: dict[str, str], key: tuple[str, ...]
) -> None:
    label = row["final_label"]
    commit = key[:2]
    package_version = key[2:]
    stats["target_rows"] += 1
    stats["commits"].add(commit)
    stats["cases"].add(key)
    stats["package_versions"].add(package_version)
    stats["label_rows"][label] += 1
    stats["label_commits"][label].add(commit)
    stats["label_cases"][label].add(key)


def scan_final_labels(
    path: Path,
    language: str,
    labels: set[str],
    cases: dict[tuple[str, ...], dict[str, Any]],
) -> dict[str, Any]:
    stats = new_scan_stats()
    for row in csv_rows(path):
        stats["scanned_rows"] += 1
        if row.get("final_label") not in labels:
            continue
        key = case_key_from_row(row)
        if key is None:
            stats["target_rows"] += 1
            stats["invalid_target_rows"] += 1
            continue
        case = cases.setdefault(key, new_case(key, language))
        add_label_row(case, row, language)
        record_scan_stats(stats, row, key)
    return stats


def commit_case_map(
    cases: dict[tuple[str, ...], dict[str, Any]], language: str
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    mapping: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for case in cases.values():
        if language in case["languages"]:
            mapping[(case["repo"], case["sha"])].append(case)
    return mapping


def add_candidate_row(case: dict[str, Any], row: dict[str, str]) -> None:
    case["candidate_found"] = True
    for name, source in CANDIDATE_FIELD_SOURCES.items():
        add_nonempty(case[name], row.get(source))


def attach_candidate_metadata(
    path: Path,
    language: str,
    cases: dict[tuple[str, ...], dict[str, Any]],
) -> int:
    mapping = commit_case_map(cases, language)
    matched_commits: set[tuple[str, str]] = set()
    for row in csv_rows(path):
        commit = (row.get("repo", "").strip().lower(), row.get("sha", "").strip().lower())
        if commit not in mapping:
            continue
        matched_commits.add(commit)
        for case in mapping[commit]:
            add_candidate_row(case, row)
    return len(matched_commits)


def json_values(values: Iterable[str]) -> str:
    return json.dumps(sorted(set(values)), ensure_ascii=False, separators=(",", ":"))


def single_value(values: set[str]) -> str:
    return next(iter(values)) if len(values) == 1 else ""


def boolean_summary(values: set[str], mode: str) -> str:
    normalized = {value.lower() for value in values if value}
    if mode == "any":
        return "true" if "true" in normalized else "false"
    return "true" if normalized and normalized == {"true"} else "false"


def base_inventory_row(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case_id_for(
            case["repo"], case["sha"], case["ecosystem"], case["package_name"], case["version"]
        ),
        "language": single_value(case["languages"]),
        "languages_json": json_values(case["languages"]),
        "repo": case["repo"],
        "sha": case["sha"],
        "ecosystem": case["ecosystem"],
        "package_name": case["package_name"],
        "version": case["version"],
        "final_label": single_value(case["final_labels"]),
        "final_labels_json": json_values(case["final_labels"]),
        "agent": single_value(case["agents"]),
        "agents_json": json_values(case["agents"]),
        "author_date": single_value(case["author_dates"]),
        "author_dates_json": json_values(case["author_dates"]),
        "row_count": case["row_count"],
    }


def candidate_inventory_fields(case: dict[str, Any]) -> dict[str, str]:
    return {
        "patch_path": single_value(case["patch_paths"]),
        "patch_paths_json": json_values(case["patch_paths"]),
        "json_path": single_value(case["json_paths"]),
        "json_paths_json": json_values(case["json_paths"]),
        "commit_primary_language": single_value(case["commit_primary_languages"]),
        "commit_primary_languages_json": json_values(case["commit_primary_languages"]),
        "manifest_touched_in_diff": boolean_summary(case["manifest_touched_values"], "any"),
        "lockfile_touched_in_diff": boolean_summary(case["lockfile_touched_values"], "any"),
        "touches_supply_chain": boolean_summary(case["supply_chain_touched_values"], "any"),
    }


def inventory_row(case: dict[str, Any]) -> dict[str, Any]:
    row = base_inventory_row(case)
    row.update(
        {
            "dependency_file_changed_any": boolean_summary(
                case["dependency_changed_values"], "any"
            ),
            "dependency_file_changed_all": boolean_summary(
                case["dependency_changed_values"], "all"
            ),
        }
    )
    row.update(candidate_inventory_fields(case))
    for name in SET_FIELD_SOURCES:
        row[f"{name}_json"] = json_values(case[name])
    return row


def final_scan_stats(stats: dict[str, Any]) -> dict[str, Any]:
    labels = {}
    for label, rows in sorted(stats["label_rows"].items()):
        labels[label] = {
            "rows": rows,
            "commits": len(stats["label_commits"][label]),
            "cases": len(stats["label_cases"][label]),
        }
    return {
        "scanned_rows": stats["scanned_rows"],
        "target_rows": stats["target_rows"],
        "invalid_target_rows": stats["invalid_target_rows"],
        "commits": len(stats["commits"]),
        "cases": len(stats["cases"]),
        "package_versions": len(stats["package_versions"]),
        "labels": labels,
    }


def language_paths(input_root: Path, language: str) -> tuple[Path, Path]:
    root = input_root / "wave01_nall" / language
    if not root.exists():
        root = input_root / language
    return (
        root / "manifest_registry" / "final_labels.csv",
        root / "stage1" / "candidate_commits.csv",
    )


def fingerprint_file(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def build_inventory(
    input_root: Path, languages: list[str], labels: Iterable[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    target_labels = set(labels)
    cases: dict[tuple[str, ...], dict[str, Any]] = {}
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "input_root": str(input_root.resolve()),
        "languages": list(languages),
        "target_labels": sorted(target_labels),
        "inputs": [],
        "summary": {},
    }
    for language in languages:
        final_path, candidate_path = language_paths(input_root, language)
        manifest["inputs"].extend(
            [fingerprint_file(final_path), fingerprint_file(candidate_path)]
        )
        stats = scan_final_labels(final_path, language, target_labels, cases)
        matched = attach_candidate_metadata(candidate_path, language, cases)
        summary = final_scan_stats(stats)
        summary["candidate_commits_matched"] = matched
        manifest["summary"][language] = summary
    rows = [inventory_row(case) for case in cases.values()]
    rows.sort(key=lambda row: (row["language"], row["repo"], row["sha"], row["package_name"], row["version"]))
    manifest["case_count"] = len(rows)
    return rows, manifest


def count_json_conflicts(rows: list[dict[str, Any]], field: str) -> int:
    return sum(len(json.loads(row[field])) != 1 for row in rows)


def validate_inventory(
    rows: list[dict[str, Any]], manifest: dict[str, Any]
) -> dict[str, Any]:
    ids = [row["case_id"] for row in rows]
    target_rows = sum(item["target_rows"] for item in manifest["summary"].values())
    aggregated_rows = sum(int(row["row_count"]) for row in rows)
    report = {
        "schema_version": SCHEMA_VERSION,
        "checked_cases": len(rows),
        "input_target_rows": target_rows,
        "aggregated_target_rows": aggregated_rows,
        "invalid_target_rows": sum(
            item["invalid_target_rows"] for item in manifest["summary"].values()
        ),
        "duplicate_case_ids": len(ids) - len(set(ids)),
        "conflicting_final_label_cases": count_json_conflicts(rows, "final_labels_json"),
        "conflicting_agent_cases": count_json_conflicts(rows, "agents_json"),
        "conflicting_author_date_cases": count_json_conflicts(rows, "author_dates_json"),
        "missing_candidate_metadata_cases": sum(
            not row["patch_path"] or not row["json_path"] for row in rows
        ),
        "row_count_mismatch": int(target_rows != aggregated_rows),
        "case_count_mismatch": int(manifest["case_count"] != len(rows)),
    }
    failures = sum(value for key, value in report.items() if key not in {"schema_version", "checked_cases", "input_target_rows", "aggregated_target_rows"})
    report["status"] = "pass" if failures == 0 else "fail"
    return report


def atomic_write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=INVENTORY_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_inventory_outputs(
    output_dir: Path,
    rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    invariants: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    inventory_path = output_dir / "case_inventory.csv"
    atomic_write_csv(inventory_path, rows)
    manifest["output"] = fingerprint_file(inventory_path)
    manifest["output"]["rows"] = len(rows)
    atomic_write_json(output_dir / "case_inventory_manifest.json", manifest)
    atomic_write_json(output_dir / "case_inventory_invariants.json", invariants)


def main() -> None:
    args = parse_args()
    rows, manifest = build_inventory(args.input_root, args.languages, args.labels)
    invariants = validate_inventory(rows, manifest)
    write_inventory_outputs(args.output_dir, rows, manifest, invariants)
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

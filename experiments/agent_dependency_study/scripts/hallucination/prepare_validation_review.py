"""Prepare a deterministic, prefilled review queue for RQ3 history candidates.

[IN]: case_inventory.csv, local_evidence.csv, github_evidence.csv, and the
merged agent commit population.
[OUT]: review_form.csv, review_selection_manifest.json,
review_preparation_manifest.json, review_preparation_invariants.json, and
public_redacted_review_queue.csv.
[POS]: Offline Phase 3 review preparation. Automatic tiers prioritize or
challenge evidence; they never fill reviewer or adjudicated labels.
[SYNC]: If review fields, calibration selection, or automatic tiers change,
update tests, scripts/OUTPUTS.md, the validation README, and the active plan.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from build_validation_cases import HISTORY_VALIDATION_LABELS, fingerprint_file


DEFAULT_ROOT = Path("data_products/rq2_hallucinated_validation_20260721")
DEFAULT_POPULATION = Path(
    "data_products/agent_commit_population_merged_1y_v1/"
    "merged_agent_commit_population.csv"
)
SCHEMA_VERSION = 1
CALIBRATION_SIZE = 10
CALIBRATION_SEED = "rq3-validation-codebook-v1"

MANUAL_FIELDS = (
    "agent_attribution_check",
    "mapping_check",
    "parent_child_check",
    "timestamp_check",
    "registry_check",
    "private_custom_local_check",
    "generated_vendored_check",
    "reviewer_1_label",
    "reviewer_1_reason",
    "reviewer_1_confidence",
    "reviewer_2_label",
    "reviewer_2_reason",
    "reviewer_2_confidence",
    "adjudicated_label",
    "adjudication_reason",
    "review_notes",
)

REVIEW_FIELDS = (
    "case_id", "redacted_case_id", "calibration_selected",
    "calibration_stratum", "language", "agent", "repo", "sha",
    "ecosystem", "package_name", "version", "pipeline_final_label",
    "author_date", "github_author_date", "github_committer_date",
    "registry_publish_time", "publish_after_author_seconds",
    "publish_after_committer_seconds", "parent_sha", "parent_count",
    "parent_child_state", "local_change_class", "dependency_file_changed",
    "package_added_line_count", "version_added_line_count",
    "source_files_json", "dependency_file_paths_json", "version_specs_json",
    "resolved_versions_json", "resolution_sources_json",
    "mapping_statuses_json", "registry_sources_json", "dependency_groups_json",
    "agent_evidence_scope", "agent_evidence_type", "agent_evidence_channels",
    "agent_evidence_tiers", "agent_evidence_modes", "agent_author_name",
    "agent_author_email", "commit_message_first_line", "commit_html_url",
    "generated_or_vendored_path_hint", "non_registry_spec_hint",
    "automatic_evidence_tier", "automatic_review_reason",
    "local_evidence_path", "github_evidence_path",
) + MANUAL_FIELDS

PUBLIC_FIELDS = (
    "redacted_case_id", "calibration_selected", "calibration_stratum",
    "language", "agent", "pipeline_final_label", "parent_count",
    "parent_child_state", "local_change_class", "dependency_file_changed",
    "publish_after_author_seconds", "publish_after_committer_seconds",
    "generated_or_vendored_path_hint", "non_registry_spec_hint",
    "automatic_evidence_tier", "automatic_review_reason",
)

GENERATED_SEGMENTS = frozenset(
    {"node_modules", "vendor", "vendored", "dist", "build", "generated"}
)
NON_REGISTRY_PREFIXES = (
    "file:", "link:", "workspace:", "git:", "git+", "github:",
    "http:", "https:", "ssh:", "npm:",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--population", type=Path, default=DEFAULT_POPULATION)
    parser.add_argument("--calibration-size", type=int, default=CALIBRATION_SIZE)
    parser.add_argument("--seed", default=CALIBRATION_SEED)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def json_values(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def after_committer(row: dict[str, str]) -> bool:
    try:
        return float(row.get("publish_after_committer_seconds", "")) > 0
    except (TypeError, ValueError):
        return False


def review_stratum(row: dict[str, str]) -> str:
    state = row.get("parent_child_state", "missing")
    return f"{row.get('language', '')}|{state}|after_committer={after_committer(row)}"


def automatic_assessment(row: dict[str, str]) -> dict[str, str]:
    state = row.get("parent_child_state", "")
    seconds = row.get("publish_after_committer_seconds", "")
    if seconds == "":
        return {"automatic_evidence_tier": "incomplete", "automatic_review_reason": "missing_committer_comparison"}
    if not after_committer(row):
        return {"automatic_evidence_tier": "contradicted_time", "automatic_review_reason": "not_after_committer"}
    if state == "preexisting_same_spec_or_version":
        return {"automatic_evidence_tier": "contradicted_parent_introduction", "automatic_review_reason": "same_state_in_parent"}
    if state in {"package_added_parent_file_missing", "version_or_spec_changed"}:
        return {"automatic_evidence_tier": "priority_high", "automatic_review_reason": "new_state_after_committer"}
    if state == "root_commit_no_parent":
        return {"automatic_evidence_tier": "priority_root", "automatic_review_reason": "root_commit_after_committer"}
    return {"automatic_evidence_tier": "needs_manual_review", "automatic_review_reason": "unclassified_parent_state"}


def stable_rank(seed: str, case_id: str) -> str:
    return hashlib.sha256(f"{seed}|{case_id}".encode("utf-8")).hexdigest()


def select_calibration_cases(
    rows: list[dict[str, str]], size: int, seed: str
) -> list[dict[str, str]]:
    if size < 1 or size > len(rows):
        raise ValueError("calibration size must be within available rows")
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[review_stratum(row)].append(row)
    if len(grouped) > size:
        raise ValueError("calibration size cannot cover every observed stratum")
    selected = [min(group, key=lambda row: stable_rank(seed, row["case_id"])) for group in grouped.values()]
    chosen = {row["case_id"] for row in selected}
    remainder = [row for row in rows if row["case_id"] not in chosen]
    remainder.sort(key=lambda row: stable_rank(seed, row["case_id"]))
    selected.extend(remainder[: size - len(selected)])
    return sorted(selected, key=lambda row: row["case_id"])


def path_hint(case: dict[str, str]) -> str:
    paths = json_values(case.get("source_files_json", ""))
    paths += json_values(case.get("dep_file_paths_json", ""))
    for path in paths:
        segments = {part.lower() for part in path.replace("\\", "/").split("/")}
        if segments & GENERATED_SEGMENTS:
            return "true"
    return "false"


def spec_hint(case: dict[str, str]) -> str:
    specs = json_values(case.get("version_specs_json", ""))
    return str(any(spec.lower().startswith(NON_REGISTRY_PREFIXES) for spec in specs)).lower()


def population_match(
    records: list[dict[str, str]], agent: str
) -> dict[str, str]:
    matches = [row for row in records if row.get("agent") == agent]
    return matches[0] if matches else (records[0] if records else {})


def build_review_row(
    case: dict[str, str], local: dict[str, str], github: dict[str, str],
    population: list[dict[str, str]], selected: bool,
) -> dict[str, str]:
    pop = population_match(population, case.get("agent", ""))
    row = {field: "" for field in REVIEW_FIELDS}
    direct = ("case_id", "language", "agent", "repo", "sha", "ecosystem", "package_name", "version", "author_date")
    row.update({field: case.get(field, "") for field in direct})
    row.update(_case_review_fields(case))
    row.update(_local_review_fields(local))
    row.update(_github_review_fields(github))
    row.update(_population_review_fields(pop))
    row.update(automatic_assessment(github))
    row["redacted_case_id"] = case.get("case_id", "")
    row["calibration_selected"] = str(selected).lower()
    row["calibration_stratum"] = review_stratum(github)
    return row


def _case_review_fields(case: dict[str, str]) -> dict[str, str]:
    mapping = {
        "pipeline_final_label": "final_label", "source_files_json": "source_files_json",
        "dependency_file_paths_json": "dep_file_paths_json", "version_specs_json": "version_specs_json",
        "resolved_versions_json": "resolved_versions_json", "resolution_sources_json": "resolution_sources_json",
        "mapping_statuses_json": "mapping_statuses_json", "registry_sources_json": "registry_sources_json",
        "dependency_groups_json": "dependency_groups_json",
    }
    result = {target: case.get(source, "") for target, source in mapping.items()}
    result["generated_or_vendored_path_hint"] = path_hint(case)
    result["non_registry_spec_hint"] = spec_hint(case)
    return result


def _local_review_fields(local: dict[str, str]) -> dict[str, str]:
    return {
        "local_change_class": local.get("local_change_class", ""),
        "dependency_file_changed": local.get("dependency_file_changed_delta", ""),
        "package_added_line_count": local.get("package_added_line_count", ""),
        "version_added_line_count": local.get("version_added_line_count", ""),
        "local_evidence_path": local.get("evidence_json_path", ""),
    }


def _github_review_fields(github: dict[str, str]) -> dict[str, str]:
    fields = (
        "github_author_date", "github_committer_date", "registry_publish_time",
        "publish_after_author_seconds", "publish_after_committer_seconds",
        "parent_sha", "parent_count", "parent_child_state",
    )
    result = {field: github.get(field, "") for field in fields}
    result["github_evidence_path"] = github.get("evidence_json_path", "")
    return result


def _population_review_fields(pop: dict[str, str]) -> dict[str, str]:
    mapping = {
        "agent_evidence_scope": "evidence_scope", "agent_evidence_type": "evidence_type",
        "agent_evidence_channels": "evidence_channels", "agent_evidence_tiers": "evidence_tiers",
        "agent_evidence_modes": "evidence_modes", "agent_author_name": "author_name",
        "agent_author_email": "author_email", "commit_message_first_line": "message_first_line",
        "commit_html_url": "html_url",
    }
    return {target: pop.get(source, "") for target, source in mapping.items()}


def public_review_row(row: dict[str, str]) -> dict[str, str]:
    return {field: row.get(field, "") for field in PUBLIC_FIELDS}


def index_unique(rows: Iterable[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row["case_id"]: row for row in rows}


def normalize_repo(value: str) -> str:
    normalized = value.strip().lower().replace("\\", "/")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    normalized = normalized.strip("/")
    return normalized[:-4] if normalized.endswith(".git") else normalized


def load_population(
    path: Path, wanted: set[tuple[str, str]]
) -> dict[tuple[str, str], list[dict[str, str]]]:
    result: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (normalize_repo(row.get("repo", "")), row.get("sha", ""))
            if key in wanted:
                result[key].append(row)
    return result


def validate_review_rows(
    rows: list[dict[str, str]], expected: int
) -> dict[str, Any]:
    ids = [row.get("case_id", "") for row in rows]
    report = {
        "schema_version": SCHEMA_VERSION, "expected_cases": expected,
        "review_rows": len(rows), "unique_case_ids": len(set(ids)),
        "missing_case_ids": sum(not value for value in ids),
        "prefilled_adjudications": sum(bool(row.get("adjudicated_label")) for row in rows),
    }
    report["status"] = "pass" if (
        len(rows) == expected == len(set(ids))
        and not report["missing_case_ids"] and not report["prefilled_adjudications"]
    ) else "fail"
    return report


def atomic_csv(path: Path, rows: list[dict[str, str]], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def prepare(args: argparse.Namespace) -> tuple[list[dict[str, str]], dict[str, Any]]:
    paths = _input_paths(args.root, args.population)
    cases = [
        row for row in read_csv(paths["inventory"])
        if row.get("final_label") in HISTORY_VALIDATION_LABELS
    ]
    local, github = index_unique(read_csv(paths["local"])), index_unique(read_csv(paths["github"]))
    wanted = {(normalize_repo(row["repo"]), row["sha"]) for row in cases}
    population = load_population(paths["population"], wanted)
    prelim = [_joined_row(row, local, github, population, False) for row in cases]
    selected = {row["case_id"] for row in select_calibration_cases(prelim, args.calibration_size, args.seed)}
    rows = [_joined_row(row, local, github, population, row["case_id"] in selected) for row in cases]
    rows.sort(key=lambda row: (row["language"], row["repo"], row["sha"], row["case_id"]))
    return rows, _metadata(rows, paths, selected, args)


def _joined_row(case: dict[str, str], local: dict[str, dict[str, str]], github: dict[str, dict[str, str]], population: dict[tuple[str, str], list[dict[str, str]]], selected: bool) -> dict[str, str]:
    case_id = case["case_id"]
    return build_review_row(
        case, local.get(case_id, {}), github.get(case_id, {}),
        population.get((normalize_repo(case["repo"]), case["sha"]), []), selected,
    )


def _input_paths(root: Path, population: Path) -> dict[str, Path]:
    return {
        "inventory": root / "case_inventory.csv", "local": root / "local_evidence.csv",
        "github": root / "github_evidence.csv", "population": population,
    }


def _metadata(rows: list[dict[str, str]], paths: dict[str, Path], selected: set[str], args: argparse.Namespace) -> dict[str, Any]:
    strata = Counter(row["calibration_stratum"] for row in rows)
    selected_strata = Counter(row["calibration_stratum"] for row in rows if row["case_id"] in selected)
    return {
        "schema_version": SCHEMA_VERSION, "generated_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed, "calibration_size": args.calibration_size,
        "selected_case_ids": sorted(selected), "strata_counts": dict(sorted(strata.items())),
        "selected_strata_counts": dict(sorted(selected_strata.items())),
        "input_fingerprints": {name: fingerprint_file(path) for name, path in paths.items()},
    }


def write_outputs(root: Path, rows: list[dict[str, str]], metadata: dict[str, Any]) -> None:
    public = [public_review_row(row) for row in rows]
    invariants = validate_review_rows(rows, expected=51)
    invariants["calibration_rows"] = sum(row["calibration_selected"] == "true" for row in rows)
    invariants["observed_strata"] = len(metadata["strata_counts"])
    invariants["covered_strata"] = len(metadata["selected_strata_counts"])
    invariants["missing_population_matches"] = sum(not row["agent_evidence_channels"] for row in rows)
    if invariants["calibration_rows"] != metadata["calibration_size"] or invariants["covered_strata"] != invariants["observed_strata"]:
        invariants["status"] = "fail"
    atomic_csv(root / "review_form.csv", rows, REVIEW_FIELDS)
    atomic_csv(root / "public_redacted_review_queue.csv", public, PUBLIC_FIELDS)
    atomic_json(root / "review_selection_manifest.json", metadata)
    atomic_json(root / "review_preparation_invariants.json", invariants)
    manifest = {"schema_version": SCHEMA_VERSION, "outputs": {
        name: fingerprint_file(root / name) for name in (
            "review_form.csv", "public_redacted_review_queue.csv",
            "review_selection_manifest.json", "review_preparation_invariants.json",
        )
    }}
    atomic_json(root / "review_preparation_manifest.json", manifest)


def main() -> None:
    args = parse_args()
    rows, metadata = prepare(args)
    write_outputs(args.root, rows, metadata)
    print(json.dumps({"review_rows": len(rows), "calibration_rows": args.calibration_size}, sort_keys=True))


if __name__ == "__main__":
    main()

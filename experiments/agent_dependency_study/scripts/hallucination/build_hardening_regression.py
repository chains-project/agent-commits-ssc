"""Build and compare the versioned 51-case RQ3 hardening regression.

[IN]: Historical validation review_form.csv, preliminary_codex_review.csv,
and Stage1 candidate_commits.csv/import_events.csv from the unlimited run.
[OUT]: Filtered per-language Stage1 inputs, regression_manifest.json, and,
after Stage2/Stage3 reruns, case_comparison.csv plus comparison_summary.json
and before_after_report.md under a new versioned data product.
[POS]: Reproducibility bridge for validating Stage2/Stage3 hardening without
overwriting the historical production or validation products.
[SYNC]: If paths, comparison fields, or transition classes change, update
scripts/OUTPUTS.md and the active strong-label hardening plan.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SOURCE_ROOT = Path("data_products/rq2_hallucinated_unlimited_overnight_20260710")
VALIDATION_ROOT = Path("data_products/rq2_hallucinated_validation_20260721")
OUTPUT_ROOT = Path("data_products/rq2_hallucinated_strong_label_hardened_20260722")
LANGUAGES = ("TypeScript", "JavaScript")
COMPARISON_FIELDS = (
    "case_id", "language", "repo", "sha", "ecosystem", "package_name",
    "version", "old_pipeline_label", "preliminary_label", "matched_new_rows",
    "new_final_labels", "new_label_reasons", "new_query_packages",
    "new_query_versions", "new_declared_packages", "new_resolution_sources",
    "new_dependency_groups", "new_dep_file_paths", "new_source_files",
    "transition_class",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--validation-root", type=Path, default=VALIDATION_ROOT)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--languages", nargs="+", default=list(LANGUAGES))
    parser.add_argument("--mode", choices=("prepare", "compare", "both"), default="prepare")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def fingerprint(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def normalize_repo(value: str) -> str:
    return (value or "").strip().lower().removesuffix(".git")


def source_stage1(root: Path, language: str) -> Path:
    wave = root / "wave01_nall" / language / "stage1"
    return wave if wave.exists() else root / language / "stage1"


def case_key(row: dict[str, str]) -> tuple[str, str]:
    return normalize_repo(row.get("repo", "")), row.get("sha", "")


def filter_rows(path: Path, wanted: set[tuple[str, str]]) -> list[dict[str, str]]:
    return [row for row in read_csv(path) if case_key(row) in wanted]


def prepare_language(
    source_root: Path, output_root: Path, language: str,
    wanted: set[tuple[str, str]],
) -> dict[str, Any]:
    source = source_stage1(source_root, language)
    target = output_root / language / "stage1"
    candidates_path, imports_path = source / "candidate_commits.csv", source / "import_events.csv"
    candidates, imports = filter_rows(candidates_path, wanted), filter_rows(imports_path, wanted)
    write_csv(target / "candidate_commits.csv", candidates[0].keys(), candidates)
    write_csv(target / "import_events.csv", imports[0].keys(), imports)
    return {
        "language": language,
        "selected_commits": len(candidates),
        "selected_import_events": len(imports),
        "requested_commits": len(wanted),
        "inputs": [fingerprint(candidates_path), fingerprint(imports_path)],
        "outputs": [
            fingerprint(target / "candidate_commits.csv"),
            fingerprint(target / "import_events.csv"),
        ],
    }


def prepare_regression_inputs(args: argparse.Namespace) -> dict[str, Any]:
    review_path = args.validation_root / "review_form.csv"
    cases = [row for row in read_csv(review_path) if row["language"] in args.languages]
    grouped: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in cases:
        grouped[row["language"]].add(case_key(row))
    summaries = [
        prepare_language(args.source_root, args.output_root, language, grouped[language])
        for language in args.languages
    ]
    write_csv(args.output_root / "regression_case_map.csv", cases[0].keys(), cases)
    manifest = {
        "schema_version": 1, "generated_utc": utc_now(),
        "source_review": fingerprint(review_path),
        "case_rows": len(cases),
        "unique_commits": len({(row["language"], *case_key(row)) for row in cases}),
        "languages": summaries,
    }
    write_json(args.output_root / "regression_manifest.json", manifest)
    return manifest


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def identity_values(row: dict[str, str]) -> set[str]:
    fields = ("query_package", "declared_package", "package_candidate", "import_root")
    return {(row.get(field) or "").lower() for field in fields if row.get(field)}


def version_values(row: dict[str, str]) -> set[str]:
    fields = ("query_version", "resolved_version", "version_spec")
    return {(row.get(field) or "").lower() for field in fields if row.get(field)}


def match_case_rows(
    case: dict[str, str], rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    package, version = case["package_name"].lower(), case["version"].lower()
    base = [
        row for row in rows
        if case_key(row) == case_key(case)
        and row.get("ecosystem", "").lower() == case.get("ecosystem", "").lower()
        and package in identity_values(row)
    ]
    exact = [row for row in base if version and version in version_values(row)]
    return exact or base


def joined_values(rows: list[dict[str, str]], field: str) -> str:
    return ";".join(sorted({row.get(field, "") for row in rows if row.get(field, "")}))


def transition_class(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "unmatched"
    sources = {row.get("resolution_source", "") for row in rows}
    labels = {row.get("final_label", "") for row in rows}
    if "first_party_module" in sources:
        return "first_party_excluded"
    alias_target_changed = any(
        row.get("query_package")
        and row.get("declared_package")
        and row["query_package"].lower() != row["declared_package"].lower()
        for row in rows
    )
    alias_group = any("npm.alias" in row.get("dependency_group", "") for row in rows)
    if "manifest_npm_alias" in sources or alias_target_changed or alias_group:
        return "alias_target_corrected"
    if labels == {"version_published_after_author_date_candidate"}:
        return "future_publish_candidate_only"
    if labels == {"version_exists_before_author_date"}:
        return "version_exists_before_author_date"
    return "other:" + ",".join(sorted(labels))


def comparison_row(
    case: dict[str, str], preliminary: dict[str, str],
    final_rows: list[dict[str, str]],
) -> dict[str, Any]:
    matched = match_case_rows(case, final_rows)
    return {
        "case_id": case["case_id"], "language": case["language"],
        "repo": case["repo"], "sha": case["sha"], "ecosystem": case["ecosystem"],
        "package_name": case["package_name"], "version": case["version"],
        "old_pipeline_label": case["pipeline_final_label"],
        "preliminary_label": preliminary.get("preliminary_label", ""),
        "matched_new_rows": len(matched),
        "new_final_labels": joined_values(matched, "final_label"),
        "new_label_reasons": joined_values(matched, "label_reason"),
        "new_query_packages": joined_values(matched, "query_package"),
        "new_query_versions": joined_values(matched, "query_version"),
        "new_declared_packages": joined_values(matched, "declared_package"),
        "new_resolution_sources": joined_values(matched, "resolution_source"),
        "new_dependency_groups": joined_values(matched, "dependency_group"),
        "new_dep_file_paths": joined_values(matched, "dep_file_path"),
        "new_source_files": joined_values(matched, "source_file"),
        "transition_class": transition_class(matched),
    }


def load_final_rows(output_root: Path, languages: list[str]) -> dict[str, list[dict[str, str]]]:
    return {
        language: read_csv(output_root / language / "manifest_registry" / "final_labels.csv")
        for language in languages
    }


def validate_comparison(
    rows: list[dict[str, Any]],
    finals: dict[str, list[dict[str, str]]],
    expected_cases: int,
) -> dict[str, Any]:
    final_rows = [row for language_rows in finals.values() for row in language_rows]
    ids = [row["case_id"] for row in rows]
    report = {
        "expected_cases": expected_cases,
        "comparison_cases": len(rows),
        "duplicate_case_ids": len(ids) - len(set(ids)),
        "unmatched_cases": sum(int(row["matched_new_rows"]) == 0 for row in rows),
        "historical_future_strong_rows": sum(
            row.get("final_label") == "version_published_after_author_date"
            for row in final_rows
        ),
        "candidate_strength_mismatches": sum(
            row.get("final_label") == "version_published_after_author_date_candidate"
            and row.get("evidence_strength") != "candidate"
            for row in final_rows
        ),
        "first_party_missing_manifest_path_cases": sum(
            row["transition_class"] == "first_party_excluded"
            and not row.get("new_dep_file_paths")
            for row in rows
        ),
        "alias_target_corrected_cases": sum(
            row["transition_class"] == "alias_target_corrected" for row in rows
        ),
        "final_label_rows": len(final_rows),
    }
    blocking = (
        report["comparison_cases"] != expected_cases
        or any(report[key] for key in (
            "duplicate_case_ids", "unmatched_cases",
            "historical_future_strong_rows", "candidate_strength_mismatches",
            "first_party_missing_manifest_path_cases",
        ))
    )
    return {"status": "fail" if blocking else "pass", **report}


def build_report(summary: dict[str, Any]) -> str:
    lines = [
        "# RQ3 strong-label hardening before/after regression", "",
        f"Generated UTC: {summary['generated_utc']}", "",
        "## Coverage", "",
        f"- Historical strong cases: {summary['cases']}",
        f"- Matched after rerun: {summary['matched_cases']}",
        f"- Unmatched after rerun: {summary['unmatched_cases']}", "",
        f"- Invariant status: {summary['invariant_status']}", "",
        "## Transition classes", "",
    ]
    lines.extend(f"- `{key}`: {value}" for key, value in summary["transition_counts"].items())
    lines += [
        "", "## Interpretation", "",
        "- The new future-publish label is a history-validation candidate, not a strong hallucination conclusion.",
        "- First-party and alias transitions test the two identity-loss mechanisms found in the historical 51-case review.",
        "- This regression does not replace first-parent, committer-time, root-history, or human adjudication.",
        "",
    ]
    return "\n".join(lines)


def compare_regression(args: argparse.Namespace) -> dict[str, Any]:
    cases = read_csv(args.validation_root / "review_form.csv")
    prelim_rows = read_csv(args.validation_root / "preliminary_codex_review.csv")
    prelim = {row["case_id"]: row for row in prelim_rows}
    finals = load_final_rows(args.output_root, args.languages)
    rows = [
        comparison_row(case, prelim.get(case["case_id"], {}), finals[case["language"]])
        for case in cases if case["language"] in finals
    ]
    counts = Counter(row["transition_class"] for row in rows)
    summary = {
        "schema_version": 1, "generated_utc": utc_now(), "cases": len(rows),
        "matched_cases": sum(row["matched_new_rows"] > 0 for row in rows),
        "unmatched_cases": sum(row["matched_new_rows"] == 0 for row in rows),
        "transition_counts": dict(sorted(counts.items())),
        "preliminary_label_counts": dict(sorted(Counter(
            row["preliminary_label"] for row in rows
        ).items())),
    }
    write_csv(args.output_root / "case_comparison.csv", COMPARISON_FIELDS, rows)
    invariants = validate_comparison(rows, finals, expected_cases=len(cases))
    invariants["inputs"] = [
        fingerprint(
            args.output_root / language / "manifest_registry" / "final_labels.csv"
        )
        for language in args.languages
    ]
    invariants["comparison_output"] = fingerprint(
        args.output_root / "case_comparison.csv"
    )
    summary["invariant_status"] = invariants["status"]
    write_json(args.output_root / "comparison_summary.json", summary)
    write_json(args.output_root / "comparison_invariants.json", invariants)
    (args.output_root / "before_after_report.md").write_text(
        build_report(summary), encoding="utf-8"
    )
    return summary


def main() -> None:
    args = parse_args()
    result: dict[str, Any] = {}
    if args.mode in {"prepare", "both"}:
        result["prepare"] = prepare_regression_inputs(args)
    if args.mode in {"compare", "both"}:
        result["compare"] = compare_regression(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

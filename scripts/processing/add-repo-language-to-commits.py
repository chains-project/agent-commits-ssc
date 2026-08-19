"""
[IN]: merged commit population CSV and repo_metadata.csv.
[OUT]: merged_agent_commit_population_with_repo_language.csv and repo_language_join_manifest.json.
[POS]: Adds repository primary language to commit rows before language-filtered diff/RQ3 analysis.
[SYNC]: If join keys, output columns, or manifest fields change, update scripts/CLAUDE.md and scripts/OUTPUTS.md.
Stream-join commit rows with repository metadata language.

Input:
  - data_products/agent_commit_population_merged_1y_v1/merged_agent_commit_population.csv
  - data_products/repo_metadata_merged_1y_v1/repo_metadata.csv

Output:
  - By default, a separate merged_agent_commit_population_with_repo_language.csv.
  - With --in-place, rewrites the input commit CSV via a temporary file and keeps
    a timestamped backup next to the original.

The joined language is GitHub repository primary language from the repository
metadata endpoint, not the full /languages byte distribution and not a
file-level commit language.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMMIT_CSV = (
    ROOT
    / "data_products"
    / "agent_commit_population_merged_1y_v1"
    / "merged_agent_commit_population.csv"
)
DEFAULT_REPO_METADATA_CSV = (
    ROOT / "data_products" / "repo_metadata_merged_1y_v1" / "repo_metadata.csv"
)
DEFAULT_OUTPUT_CSV = (
    ROOT
    / "data_products"
    / "agent_commit_population_merged_1y_v1"
    / "merged_agent_commit_population_with_repo_language.csv"
)
DEFAULT_MANIFEST = (
    ROOT
    / "data_products"
    / "agent_commit_population_merged_1y_v1"
    / "repo_language_join_manifest.json"
)


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add GitHub repository primary language to commit CSV rows."
    )
    parser.add_argument("--commit-csv", type=Path, default=DEFAULT_COMMIT_CSV)
    parser.add_argument("--repo-metadata-csv", type=Path, default=DEFAULT_REPO_METADATA_CSV)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--force", action="store_true", help="Overwrite output if it exists.")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Add repo_language to the input commit CSV itself, keeping a backup.",
    )
    return parser.parse_args()


def read_repo_metadata(path: Path) -> dict[str, tuple[str, str]]:
    repo_language: dict[str, tuple[str, str]] = {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"repo", "status", "language"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"repo metadata missing columns: {sorted(missing)}")
        for row in reader:
            repo = (row.get("repo") or "").strip()
            if not repo:
                continue
            language = (row.get("language") or "").strip()
            status = (row.get("status") or "").strip()
            repo_language[repo] = (language, status)
    return repo_language


def enrich_commits(
    commit_csv: Path,
    output_csv: Path,
    repo_metadata: dict[str, tuple[str, str]],
) -> dict[str, object]:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    missing_repo_metadata = 0
    blank_language = 0
    language_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()

    with commit_csv.open("r", newline="", encoding="utf-8-sig") as src:
        reader = csv.DictReader(src)
        if not reader.fieldnames:
            raise ValueError("commit CSV has no header")
        if "repo" not in reader.fieldnames:
            raise ValueError("commit CSV missing column: repo")

        fieldnames = list(reader.fieldnames)
        if "repo_language" not in fieldnames:
            fieldnames.append("repo_language")

        with output_csv.open("w", newline="", encoding="utf-8") as dst:
            writer = csv.DictWriter(dst, fieldnames=fieldnames)
            writer.writeheader()
            for row in reader:
                repo = (row.get("repo") or "").strip()
                language, status = repo_metadata.get(repo, ("", "missing_repo_metadata"))
                if status == "missing_repo_metadata":
                    missing_repo_metadata += 1
                if not language:
                    blank_language += 1
                row["repo_language"] = language
                writer.writerow(row)
                row_count += 1
                language_counts[language or "<blank>"] += 1
                status_counts[status or "<blank>"] += 1

    return {
        "rows": row_count,
        "missing_repo_metadata_rows": missing_repo_metadata,
        "blank_repo_language_rows": blank_language,
        "top_repo_languages": language_counts.most_common(30),
        "repo_metadata_status_counts": status_counts.most_common(),
    }


def main() -> None:
    args = parse_args()
    final_output_csv = args.commit_csv if args.in_place else args.output_csv
    if args.in_place:
        args.output_csv = args.commit_csv.with_suffix(args.commit_csv.suffix + ".tmp_repo_language")
    if args.output_csv.exists() and not args.force:
        raise FileExistsError(f"output exists; pass --force to overwrite: {args.output_csv}")

    repo_metadata = read_repo_metadata(args.repo_metadata_csv)
    stats = enrich_commits(args.commit_csv, args.output_csv, repo_metadata)
    backup_csv = ""
    if args.in_place:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = args.commit_csv.with_name(f"{args.commit_csv.stem}.bak_before_repo_language_{stamp}{args.commit_csv.suffix}")
        shutil.copy2(args.commit_csv, backup_path)
        args.output_csv.replace(args.commit_csv)
        backup_csv = str(backup_path)
    manifest = {
        "commit_csv": display_path(args.commit_csv),
        "repo_metadata_csv": display_path(args.repo_metadata_csv),
        "output_csv": display_path(final_output_csv),
        "in_place": args.in_place,
        "backup_csv": display_path(Path(backup_csv)) if backup_csv else "",
        "repo_language_semantics": "GitHub repository primary language from repo metadata field 'language'",
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo_metadata_repos": len(repo_metadata),
        **stats,
    }
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote={args.commit_csv if args.in_place else args.output_csv}")
    if backup_csv:
        print(f"backup={backup_csv}")
    print(f"manifest={args.manifest}")
    print(f"rows={stats['rows']}")
    print(f"blank_repo_language_rows={stats['blank_repo_language_rows']}")
    print(f"missing_repo_metadata_rows={stats['missing_repo_metadata_rows']}")


if __name__ == "__main__":
    main()

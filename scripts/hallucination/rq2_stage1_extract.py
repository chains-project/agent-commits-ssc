"""Run the compact, offline RQ2 v2 Stage 1 event extraction pipeline.

[IN]: commit_index.csv, merged agent population CSV, and local unified patches.
[OUT]: commits.csv, events.csv, schema_manifest.json, stage1_summary.json, and
stage1_report.md in a new or empty output directory.
[POS]: Deterministic first-order Stage 1 entry point. It selects commits with
external import or direct manifest-addition events and never accesses networks.
[SYNC]: Keep rq2_stage1_manifest_diff.py, rq2_stage_schema.py,
scripts/OUTPUTS.md, and the Phase 6 plan synchronized with output changes.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, TypeVar

from rq2_stage1_manifest_diff import (
    TARGET_SOURCE_LANGUAGES,
    Stage1Observation,
    actual_changed_languages,
    dependency_quadrant,
    extract_manifest_additions,
    extract_source_imports,
    parse_unified_diff,
    source_ecosystem,
)
from rq2_stage_schema import (
    SCHEMA_VERSION,
    build_schema_manifest,
    make_commit_id,
    table_contract,
    validate_row,
)
from rq2_stage1_streaming import Stage1Ops, run_stage1_streaming


DEFAULT_DATA_ROOT = Path(os.environ.get("THESIS_DATA_ROOT", "data_external"))
DEFAULT_INDEX = (
    DEFAULT_DATA_ROOT
    / "diff_corpus"
    / "commit_json_diffs_by_language"
    / "commit_index.csv"
)
DEFAULT_POPULATION = Path(
    "data_products/agent_commit_population_merged_1y_v1/merged_agent_commit_population.csv"
)
DEFAULT_OUTPUT = Path("data_products/rq2_stage1_v2")
RESOURCE_RETRY_ATTEMPTS = 10
RESOURCE_RETRY_BASE_SECONDS = 0.05
RESOURCE_RETRY_MAX_SECONDS = 0.8
RESOURCE_RETRY_WINERRORS = frozenset({1450})
RetryResult = TypeVar("RetryResult")


@dataclass(frozen=True)
class Stage1Config:
    commit_index: Path
    population_csv: Path
    output_dir: Path
    repo_languages: tuple[str, ...] = TARGET_SOURCE_LANGUAGES
    actual_languages: tuple[str, ...] = ()
    scan_all_repo_languages: bool = False
    sample_per_repo_language: int = 100
    max_index_rows: int = 0
    max_total_candidates: int = 0
    resume: bool = False
    checkpoint_every: int = 1000
    max_buffer_rows: int = 100_000


@dataclass(frozen=True)
class CommitExtraction:
    commit_row: dict[str, str]
    event_rows: tuple[dict[str, str], ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit-index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--population-csv", type=Path, default=DEFAULT_POPULATION)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--repo-languages", nargs="*", default=list(TARGET_SOURCE_LANGUAGES))
    parser.add_argument("--actual-languages", nargs="*", default=[])
    parser.add_argument("--scan-all-repo-languages", action="store_true")
    parser.add_argument("--sample-per-repo-language", type=int, default=100)
    parser.add_argument("--max-index-rows", type=int, default=0)
    parser.add_argument("--max-total-candidates", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=1000)
    parser.add_argument("--max-buffer-rows", type=int, default=100_000)
    return parser.parse_args()


def config_from_args(args: argparse.Namespace) -> Stage1Config:
    return Stage1Config(
        commit_index=args.commit_index,
        population_csv=args.population_csv,
        output_dir=args.output_dir,
        repo_languages=tuple(args.repo_languages),
        actual_languages=tuple(args.actual_languages),
        scan_all_repo_languages=args.scan_all_repo_languages,
        sample_per_repo_language=max(0, args.sample_per_repo_language),
        max_index_rows=max(0, args.max_index_rows),
        max_total_candidates=max(0, args.max_total_candidates),
        resume=args.resume,
        checkpoint_every=max(1, args.checkpoint_every),
        max_buffer_rows=max(1, args.max_buffer_rows),
    )


def _with_resource_retry(operation: Callable[[], RetryResult]) -> RetryResult:
    for attempt in range(RESOURCE_RETRY_ATTEMPTS):
        try:
            return operation()
        except OSError as error:
            if getattr(error, "winerror", None) not in RESOURCE_RETRY_WINERRORS:
                raise
            if attempt + 1 == RESOURCE_RETRY_ATTEMPTS:
                raise
            delay = min(
                RESOURCE_RETRY_BASE_SECONDS * (2 ** attempt),
                RESOURCE_RETRY_MAX_SECONDS,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable resource retry state")


def _open_text(path: Path):
    return _with_resource_retry(
        lambda: path.open(
            "r", encoding="utf-8-sig", errors="replace", newline="",
        )
    )


def clean_lines(path: Path) -> Iterator[str]:
    with _open_text(path) as handle:
        for line in handle:
            yield line.replace("\x00", "")


def load_population(path: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(clean_lines(path)):
        key = row.get("repo_sha") or commit_key(row)
        if key and key not in result:
            result[key] = {
                "agent": row.get("agent", ""),
                "author_date": row.get("author_date", ""),
            }
    return result


def commit_key(row: dict[str, str]) -> str:
    return f"{row.get('repo', '')}|{row.get('sha', '')}"


def iter_index_rows(path: Path, max_rows: int) -> Iterator[dict[str, str]]:
    processed = 0
    for row in csv.DictReader(clean_lines(path)):
        if row.get("status") != "ok":
            continue
        processed += 1
        row["_stage1_index_position"] = str(processed)
        yield row
        if max_rows and processed >= max_rows:
            return


def extract_commit(
    row: dict[str, str], meta: dict[str, str],
    selected_languages: tuple[str, ...] = (),
) -> CommitExtraction | None:
    patch_path = Path(row.get("patch_path", ""))
    if not _with_resource_retry(patch_path.is_file):
        return None
    file_patches = parse_unified_diff(clean_lines(patch_path))
    changed = actual_changed_languages(file_patches)
    observations = _observations(file_patches, selected_languages)
    if not observations:
        return None
    commit_id = make_commit_id(row.get("repo", ""), row.get("sha", ""))
    event_rows = tuple(event.to_event_row(commit_id) for event in observations)
    commit_row = _commit_row(row, meta, commit_id, changed, observations, selected_languages)
    return CommitExtraction(commit_row, event_rows)


def _observations(
    file_patches: Iterable[object], selected: tuple[str, ...] = (),
) -> list[Stage1Observation]:
    observations: list[Stage1Observation] = []
    allowed = set(selected)
    ecosystems = {source_ecosystem(language) for language in allowed}
    for file_patch in file_patches:
        sources = extract_source_imports(file_patch)
        manifests = extract_manifest_additions(file_patch)
        observations.extend(item for item in sources if not allowed or item.actual_language in allowed)
        observations.extend(item for item in manifests if not allowed or item.ecosystem in ecosystems)
    return observations


def _commit_row(
    row: dict[str, str],
    meta: dict[str, str],
    commit_id: str,
    changed_languages: tuple[str, ...],
    observations: list[Stage1Observation],
    selected_languages: tuple[str, ...] = (),
) -> dict[str, str]:
    import_count = sum(event.event_type == "import" for event in observations)
    manifest_count = sum(event.event_type == "manifest_addition" for event in observations)
    allowed = set(selected_languages)
    languages = tuple(item for item in changed_languages if not allowed or item in allowed)
    return {
        "commit_id": commit_id,
        "repo": row.get("repo", ""),
        "sha": row.get("sha", ""),
        "author_date": meta.get("author_date", ""),
        "agent": meta.get("agent", ""),
        "repo_primary_language": _repo_language(row),
        "actual_changed_languages": "|".join(languages),
        "import_event_count": str(import_count),
        "manifest_event_count": str(manifest_count),
        "dependency_quadrant": dependency_quadrant(observations),
        "schema_version": str(SCHEMA_VERSION),
    }


def _repo_language(row: dict[str, str]) -> str:
    return row.get("repo_language") or row.get("commit_primary_language", "")


def row_language_allowed(row: dict[str, str], config: Stage1Config) -> bool:
    if config.actual_languages:
        # commit_languages is only an index hint; formal mode routes each patch file.
        return True
    if config.scan_all_repo_languages:
        return True
    return _repo_language(row) in set(config.repo_languages)


def run_stage1(config: Stage1Config) -> dict[str, object]:
    extractor = extract_commit
    if config.actual_languages:
        extractor = lambda row, meta: extract_commit(row, meta, config.actual_languages)
    operations = Stage1Ops(
        load_population, iter_index_rows, row_language_allowed, extractor,
        commit_key, _repo_language, _summary_config, write_report,
    )
    return run_stage1_streaming(config, operations)


def _sample_full(current: int, limit: int) -> bool:
    return bool(limit and current >= limit)


def prepare_output_dir(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty Stage 1 output: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _write_outputs(
    config: Stage1Config,
    commits: list[dict[str, str]],
    events: list[dict[str, str]],
    counters: Counter,
) -> dict[str, object]:
    write_table(config.output_dir / "commits.csv", commits)
    write_table(config.output_dir / "events.csv", events)
    manifest = build_schema_manifest(config.output_dir, ["commits.csv", "events.csv"])
    atomic_write_json(config.output_dir / "schema_manifest.json", manifest)
    summary = build_summary(config, commits, events, counters)
    atomic_write_json(config.output_dir / "stage1_summary.json", summary)
    write_report(config.output_dir / "stage1_report.md", summary)
    return summary


def write_table(path: Path, rows: list[dict[str, str]]) -> None:
    contract = table_contract(path.name)
    for row in rows:
        validate_row(path.name, row)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=contract.fields)
        writer.writeheader()
        writer.writerows(rows)


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def build_summary(
    config: Stage1Config,
    commits: list[dict[str, str]],
    events: list[dict[str, str]],
    counters: Counter,
) -> dict[str, object]:
    quadrants = Counter(row["dependency_quadrant"] for row in commits)
    repo_languages = Counter(row["repo_primary_language"] for row in commits)
    actual_languages = _actual_language_counts(commits)
    event_types = Counter(row["event_type"] for row in events)
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION,
        "mode": "offline_deterministic_first",
        "network_access": False,
        "candidate_commits": len(commits),
        "events": len(events),
        "event_types": dict(sorted(event_types.items())),
        "quadrants": dict(sorted(quadrants.items())),
        "repo_primary_languages": dict(sorted(repo_languages.items())),
        "actual_changed_languages": dict(sorted(actual_languages.items())),
        "counters": dict(sorted(counters.items())),
        "config": _summary_config(config),
    }


def _actual_language_counts(commits: Iterable[dict[str, str]]) -> Counter:
    counts = Counter()
    for row in commits:
        for language in row["actual_changed_languages"].split("|"):
            if language:
                counts[language] += 1
    return counts


def _summary_config(config: Stage1Config) -> dict[str, object]:
    return {
        "repo_languages": list(config.repo_languages),
        "actual_languages": list(config.actual_languages),
        "scan_all_repo_languages": config.scan_all_repo_languages,
        "sample_per_repo_language": config.sample_per_repo_language,
        "max_index_rows": config.max_index_rows,
        "max_total_candidates": config.max_total_candidates,
    }


def write_report(path: Path, summary: dict[str, object]) -> None:
    lines = [
        "# RQ2 Stage 1 v2 Offline Extraction Report",
        "",
        "Generated by `rq2_stage1_extract.py` without network access.",
        "",
        f"- Candidate commits：{summary['candidate_commits']}",
        f"- Events：{summary['events']}",
        f"- Event types：`{json.dumps(summary['event_types'], ensure_ascii=False)}`",
        f"- Quadrants：`{json.dumps(summary['quadrants'], ensure_ascii=False)}`",
        "",
        "Stage 1 writes only `commits.csv` and `events.csv`; Stage 2 creates episodes and links.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    config = config_from_args(parse_args())
    summary = run_stage1(config)
    print(f"candidate_commits={summary['candidate_commits']}")
    print(f"events={summary['events']}")
    print(f"output_dir={config.output_dir}")


if __name__ == "__main__":
    main()

"""Run compact, offline RQ2 v2 Stage 2 episode construction.

[IN]: commits.csv, events.csv, and Stage 1 schema_manifest.json in one directory.
[OUT]: episodes.csv, episode_event_links.csv, a four-table schema_manifest.json,
stage2_summary.json, and stage2_report.md in the same directory.
[POS]: Deterministic offline Stage 2 entry point. It never modifies Stage 1 CSVs,
never overwrites prior Stage 2 outputs, and never accesses networks.
[SYNC]: Keep rq2_stage2_episode_linker.py, rq2_stage_schema.py,
scripts/OUTPUTS.md, scripts/CLAUDE.md, and the Phase 7 plan synchronized.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from rq2_stage2_context import NullContextLookup, SqliteContextLookup
from rq2_stage2_episode_linker import build_episode_tables
from rq2_stage2_streaming import run_stage2_streaming
from rq2_stage_schema import (
    SCHEMA_VERSION,
    build_schema_manifest,
    table_contract,
    validate_row,
)


DEFAULT_INPUT = Path("data_products/rq2_stage1_v2")
STAGE2_TARGETS = (
    "episodes.csv", "episode_event_links.csv",
    "stage2_summary.json", "stage2_report.md",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=1000)
    parser.add_argument("--max-buffer-rows", type=int, default=100_000)
    parser.add_argument("--context-db", type=Path)
    return parser.parse_args()


def read_table(directory: Path, filename: str) -> list[dict[str, str]]:
    path = directory / filename
    contract = table_contract(filename)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != contract.fields:
            raise ValueError(f"invalid header for {filename}")
        rows = list(reader)
    for row in rows:
        validate_row(filename, row)
    return rows


def run_stage2(
    input_dir: Path, *, resume: bool = False,
    checkpoint_every: int = 1000, max_buffer_rows: int = 100_000,
    context_db: Path | None = None, context_lookup=None,
) -> dict[str, object]:
    lookup = context_lookup or (
        SqliteContextLookup(context_db) if context_db else NullContextLookup()
    )
    try:
        return run_stage2_streaming(
            input_dir, build_episode_tables, write_report, resume=resume,
            checkpoint_every=checkpoint_every, max_buffer_rows=max_buffer_rows,
            context_lookup=lookup,
        )
    finally:
        if context_lookup is None:
            lookup.close()

def prepare_stage2(directory: Path) -> None:
    required = [directory / name for name in ("commits.csv", "events.csv", "schema_manifest.json")]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing Stage 1 inputs: {missing}")
    existing = [name for name in STAGE2_TARGETS if (directory / name).exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite Stage 2 outputs: {existing}")


def _stage1_fingerprints(directory: Path) -> dict[str, str]:
    return {
        filename: _sha256(directory / filename)
        for filename in ("commits.csv", "events.csv")
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_stage1_counts(commits, events) -> None:
    by_commit: dict[str, Counter] = {}
    for event in events:
        by_commit.setdefault(event["commit_id"], Counter())[event["event_type"]] += 1
    for commit in commits:
        counts = by_commit.get(commit["commit_id"], Counter())
        expected = (int(commit["import_event_count"]), int(commit["manifest_event_count"]))
        actual = (counts["import"], counts["manifest_addition"])
        if expected != actual:
            raise ValueError(f"Stage 1 event counts disagree for {commit['commit_id']}")


def write_table_atomic(path: Path, rows: Iterable[Mapping[str, str]]) -> None:
    contract = table_contract(path.name)
    materialized = [dict(row) for row in rows]
    for row in materialized:
        validate_row(path.name, row)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=contract.fields)
        writer.writeheader()
        writer.writerows(materialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def build_summary(commits, events, episodes, links, before, directory) -> dict[str, object]:
    after = _stage1_fingerprints(directory)
    if before != after:
        raise RuntimeError("Stage 2 modified Stage 1 tables")
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION,
        "mode": "offline_deterministic_alignment",
        "network_access": False,
        "commits": len(commits), "events": len(events),
        "episodes": len(episodes), "episode_event_links": len(links),
        "quadrants": _counts(episodes, "dependency_quadrant"),
        "alignment_statuses": _counts(episodes, "alignment_status"),
        "query_kinds": _counts(episodes, "query_kind"),
        "stage1_table_sha256": after,
        "invariants": {
            "stage1_tables_unchanged": True,
            "four_table_manifest_complete": True,
            "network_access": False,
        },
    }


def _counts(rows: Iterable[Mapping[str, str]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(row[field] for row in rows).items()))


def write_report(path: Path, summary: Mapping[str, object]) -> None:
    lines = [
        "# RQ2 Stage 2 v2 Offline Episode Build Report", "",
        "Generated by `rq2_stage2_build.py` without network access.", "",
        f"- Commits: {summary['commits']}", f"- Events: {summary['events']}",
        f"- Dependency episodes：{summary['episodes']}",
        f"- Episode-event links：{summary['episode_event_links']}",
        f"- Quadrants：`{json.dumps(summary['quadrants'], ensure_ascii=False)}`",
        f"- Query kinds：`{json.dumps(summary['query_kinds'], ensure_ascii=False)}`",
        "", "Stage 2 builds alignment and query plans; Stage 3 updates registry and advisory states.", "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    summary = run_stage2(
        args.input_dir, resume=args.resume,
        checkpoint_every=args.checkpoint_every,
        max_buffer_rows=args.max_buffer_rows, context_db=args.context_db,
    )
    print(f"episodes={summary['episodes']}")
    print(f"episode_event_links={summary['episode_event_links']}")
    print(f"input_dir={args.input_dir}")


if __name__ == "__main__":
    main()

"""Bounded-memory execution layer for RQ2 v2 Stage 1.

[IN]: Stage1Config plus parser/population callbacks from stage1_extract.py.
[OUT]: Streamed commits/events CSVs, resumable state, schema, summary, and report.
[POS]: Stage 1 storage orchestration; parsing policy remains in existing modules.
[SYNC]: Keep stage1_extract.py, streaming tests, scripts/CLAUDE.md, and
scripts/OUTPUTS.md synchronized.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from stage_schema import SCHEMA_VERSION, build_schema_manifest, table_contract, validate_row
from streaming_store import (
    CheckpointedCsvStore,
    CsvTarget,
    atomic_write_json,
    file_signature,
)


FINAL_OUTPUTS = ("schema_manifest.json", "stage1_summary.json", "stage1_report_zh.md")


@dataclass(frozen=True)
class Stage1Ops:
    load_population: Callable
    iter_index_rows: Callable
    row_language_allowed: Callable
    extract_commit: Callable
    commit_key: Callable
    repo_language: Callable
    summary_config: Callable
    write_report: Callable


def run_stage1_streaming(config, ops: Stage1Ops) -> dict[str, object]:
    _prepare_directory(config.output_dir, config.resume)
    store = _store(config)
    state = store.prepare()
    if state.get("complete"):
        return _read_complete_summary(config.output_dir)
    try:
        return _run_incomplete(config, ops, store, state)
    except Exception:
        if not config.resume:
            store.abort_fresh()
            _cleanup_final_outputs(config.output_dir)
        raise


def _run_incomplete(config, ops, store, state) -> dict[str, object]:
    population = ops.load_population(config.population_csv)
    counters = _counter(state, "counters")
    sampled = _counter(state, "sampled")
    summary_counts = _summary_counters(state)
    batches = {"commits": [], "events": []}
    processed = int(state["processed_groups"])
    checkpointed = processed
    for position, row in enumerate(
        ops.iter_index_rows(config.commit_index, config.max_index_rows), start=1
    ):
        if position <= processed:
            continue
        if _candidate_limit_reached(state, batches, config.max_total_candidates):
            break
        processed = position
        extraction = _process_row(row, config, ops, population, counters, sampled)
        if extraction:
            _record_extraction(extraction, batches, summary_counts, ops.repo_language(row))
        if _checkpoint_due(processed, checkpointed, batches, config):
            state = _commit(store, processed, batches, counters, sampled, summary_counts)
            batches, checkpointed = {"commits": [], "events": []}, processed
    state = _commit(
        store, processed, batches, counters, sampled, summary_counts, data_complete=True
    )
    summary = _finalize(config, ops, state)
    store.mark_complete()
    return summary


def _process_row(row, config, ops, population, counters, sampled):
    counters["processed_index_rows"] += 1
    if not ops.row_language_allowed(row, config):
        counters["filtered_repo_language"] += 1
        return None
    language = ops.repo_language(row)
    if config.sample_per_repo_language and sampled[language] >= config.sample_per_repo_language:
        counters["sample_limit_skips"] += 1
        return None
    extraction = ops.extract_commit(row, population.get(ops.commit_key(row), {}))
    if extraction is None:
        counters["non_candidate_or_missing_patch"] += 1
        return None
    sampled[language] += 1
    return extraction


def _record_extraction(extraction, batches, counts, repo_language) -> None:
    validate_row("commits.csv", extraction.commit_row)
    for event in extraction.event_rows:
        validate_row("events.csv", event)
    batches["commits"].append(extraction.commit_row)
    batches["events"].extend(extraction.event_rows)
    counts["quadrants"][extraction.commit_row["dependency_quadrant"]] += 1
    counts["repo_languages"][repo_language] += 1
    for language in extraction.commit_row["actual_changed_languages"].split("|"):
        if language:
            counts["actual_languages"][language] += 1
    for event in extraction.event_rows:
        counts["event_types"][event["event_type"]] += 1


def _checkpoint_due(processed, checkpointed, batches, config) -> bool:
    by_groups = config.checkpoint_every > 0 and processed - checkpointed >= config.checkpoint_every
    buffered = len(batches["commits"]) + len(batches["events"])
    by_rows = config.max_buffer_rows > 0 and buffered >= config.max_buffer_rows
    return by_groups or by_rows


def _commit(store, processed, batches, counters, sampled, counts, data_complete=False):
    extra = {
        "counters": _plain(counters), "sampled": _plain(sampled),
        "event_types": _plain(counts["event_types"]),
        "quadrants": _plain(counts["quadrants"]),
        "repo_primary_languages": _plain(counts["repo_languages"]),
        "actual_changed_languages": _plain(counts["actual_languages"]),
    }
    return store.commit(processed, batches, extra=extra, data_complete=data_complete)


def _finalize(config, ops, state) -> dict[str, object]:
    manifest = build_schema_manifest(config.output_dir, ["commits.csv", "events.csv"])
    atomic_write_json(config.output_dir / "schema_manifest.json", manifest)
    summary = _build_summary(config, ops, state)
    atomic_write_json(config.output_dir / "stage1_summary.json", summary)
    ops.write_report(config.output_dir / "stage1_report_zh.md", summary)
    return summary


def _build_summary(config, ops, state) -> dict[str, object]:
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION, "mode": "offline_deterministic_first_streaming",
        "network_access": False,
        "candidate_commits": int(state["candidate_commits"]),
        "events": int(state["events"]),
        "event_types": state.get("event_types", {}),
        "quadrants": state.get("quadrants", {}),
        "repo_primary_languages": state.get("repo_primary_languages", {}),
        "actual_changed_languages": state.get("actual_changed_languages", {}),
        "counters": state.get("counters", {}), "config": ops.summary_config(config),
        "state_file": "stage1_state.json",
    }


def _store(config) -> CheckpointedCsvStore:
    targets = {
        "commits": _target("commits.csv", "candidate_commits"),
        "events": _target("events.csv", "events"),
    }
    seed = {
        "stage": "rq2_stage1",
        "input_signature": {
            "commit_index": file_signature(config.commit_index),
            "population": file_signature(config.population_csv),
        },
        "run_config": _resume_config(config),
    }
    return CheckpointedCsvStore(
        config.output_dir, targets, "stage1_state.json",
        resume=config.resume, seed=seed,
    )


def _target(filename: str, count_key: str) -> CsvTarget:
    return CsvTarget(filename, table_contract(filename).fields, count_key)


def _resume_config(config) -> dict[str, object]:
    return {
        "repo_languages": list(config.repo_languages),
        "actual_languages": list(config.actual_languages),
        "scan_all_repo_languages": config.scan_all_repo_languages,
        "sample_per_repo_language": config.sample_per_repo_language,
        "max_index_rows": config.max_index_rows,
        "max_total_candidates": config.max_total_candidates,
    }


def _summary_counters(state) -> dict[str, Counter]:
    return {
        "event_types": _counter(state, "event_types"),
        "quadrants": _counter(state, "quadrants"),
        "repo_languages": _counter(state, "repo_primary_languages"),
        "actual_languages": _counter(state, "actual_changed_languages"),
    }


def _counter(state: Mapping[str, object], key: str) -> Counter:
    value = state.get(key, {})
    return Counter(value if isinstance(value, Mapping) else {})


def _plain(counter: Counter) -> dict[str, int]:
    return dict(sorted(counter.items()))


def _candidate_limit_reached(state, batches, limit: int) -> bool:
    return bool(limit and int(state["candidate_commits"]) + len(batches["commits"]) >= limit)


def _prepare_directory(path: Path, resume: bool) -> None:
    if path.exists() and any(path.iterdir()) and not resume:
        raise FileExistsError(f"refusing to overwrite non-empty Stage 1 output: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _read_complete_summary(output_dir: Path) -> dict[str, object]:
    path = output_dir / "stage1_summary.json"
    if not path.is_file():
        raise FileNotFoundError(f"complete Stage 1 state missing summary: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _cleanup_final_outputs(output_dir: Path) -> None:
    for name in FINAL_OUTPUTS:
        (output_dir / name).unlink(missing_ok=True)

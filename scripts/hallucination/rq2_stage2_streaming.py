"""Grouped bounded-memory execution layer for RQ2 v2 Stage 2.

[IN]: Ordered Stage 1 commits/events plus the pure per-commit episode linker.
[OUT]: Streamed episodes/links, resumable state, four-table manifest, and report.
[POS]: Stage 2 storage orchestration; alignment policy remains in pure modules.
[SYNC]: Keep rq2_stage2_build.py, linker tests, scripts/CLAUDE.md, and
scripts/OUTPUTS.md synchronized.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from rq2_stage2_context import NullContextLookup, derived_context_rows
from rq2_stage_schema import SCHEMA_VERSION, build_schema_manifest, table_contract, validate_row
from rq2_streaming_store import (
    CheckpointedCsvStore,
    CsvTarget,
    OrderedCsvGroupCursor,
    atomic_write_json,
    iter_csv_rows,
)


FINAL_OUTPUTS = ("stage2_summary.json", "stage2_report.md")


def run_stage2_streaming(
    input_dir: Path, linker: Callable, report_writer: Callable, *,
    resume: bool = False, checkpoint_every: int = 1000,
    max_buffer_rows: int = 100_000, context_lookup=None,
) -> dict[str, object]:
    _prepare_inputs(input_dir, resume)
    context_lookup = context_lookup or NullContextLookup()
    before = _stage1_hashes(input_dir)
    input_counts = _stage1_counts(input_dir)
    store = _store(input_dir, resume, before, input_counts, context_lookup.signature)
    state = store.prepare()
    if state.get("complete"):
        return _read_complete_summary(input_dir)
    try:
        return _run_incomplete(
            input_dir, linker, report_writer, store, state,
            before, input_counts, checkpoint_every, max_buffer_rows, context_lookup,
        )
    except Exception:
        if not resume:
            store.abort_fresh()
            _cleanup_final_outputs(input_dir)
        raise


def _run_incomplete(
    input_dir, linker, report_writer, store, state,
    before, input_counts, checkpoint_every, max_buffer_rows, context_lookup,
):
    batches = {"episodes": [], "links": []}
    counters = _summary_counters(state)
    processed = int(state["processed_groups"])
    commit_fields = table_contract("commits.csv").fields
    event_fields = table_contract("events.csv").fields
    with OrderedCsvGroupCursor(
        input_dir / "events.csv", ("commit_id",), event_fields
    ) as events:
        state, batches, processed = _consume_commits(
            input_dir, commit_fields, events, linker, store, state, batches,
            counters, processed, checkpoint_every, max_buffer_rows, context_lookup,
        )
        if not events.exhausted:
            raise ValueError("events.csv order does not match commits.csv")
    state = _commit(store, processed, batches, counters, data_complete=True)
    summary = _finalize(input_dir, state, before, input_counts, report_writer)
    store.mark_complete()
    return summary


def _consume_commits(
    input_dir, commit_fields, events, linker, store, state, batches,
    counters, processed, checkpoint_every, max_buffer_rows, context_lookup,
):
    checkpointed = processed
    rows = iter_csv_rows(input_dir / "commits.csv", commit_fields)
    for index, commit in enumerate(rows, start=1):
        group = events.take((commit["commit_id"],))
        if index <= processed:
            continue
        validate_row("commits.csv", commit)
        _check_commit_counts(commit, group)
        contexts = context_lookup.lookup(commit)
        contexts.extend(derived_context_rows(commit, group))
        episodes, links = linker([commit], group, contexts)
        _extend_batches(batches, episodes, links, counters)
        processed = index
        if _checkpoint_due(
            processed, checkpointed, batches, checkpoint_every, max_buffer_rows
        ):
            state = _commit(store, processed, batches, counters)
            batches, checkpointed = {"episodes": [], "links": []}, processed
    return state, batches, processed
def _check_commit_counts(commit, events) -> None:
    counts = Counter(row["event_type"] for row in events)
    expected = (int(commit["import_event_count"]), int(commit["manifest_event_count"]))
    actual = (counts["import"], counts["manifest_addition"])
    if expected != actual:
        raise ValueError(f"Stage 1 event counts disagree for {commit['commit_id']}")


def _extend_batches(batches, episodes, links, counters) -> None:
    batches["episodes"].extend(episodes)
    batches["links"].extend(links)
    counters["quadrants"].update(row["dependency_quadrant"] for row in episodes)
    counters["alignment"].update(row["alignment_status"] for row in episodes)
    counters["query_kinds"].update(row["query_kind"] for row in episodes)


def _checkpoint_due(processed, checkpointed, batches, group_limit, row_limit) -> bool:
    by_groups = group_limit > 0 and processed - checkpointed >= group_limit
    buffered = len(batches["episodes"]) + len(batches["links"])
    by_rows = row_limit > 0 and buffered >= row_limit
    return by_groups or by_rows


def _commit(store, processed, batches, counters, data_complete=False):
    extra = {
        "quadrants": _plain(counters["quadrants"]),
        "alignment_statuses": _plain(counters["alignment"]),
        "query_kinds": _plain(counters["query_kinds"]),
    }
    return store.commit(
        processed, batches, extra=extra, data_complete=data_complete
    )


def _finalize(input_dir, state, before, input_counts, report_writer) -> dict[str, object]:
    after = _stage1_hashes(input_dir)
    if before != after:
        raise RuntimeError("Stage 2 modified Stage 1 tables")
    manifest = build_schema_manifest(input_dir)
    summary = _build_summary(state, after, input_counts)
    atomic_write_json(input_dir / "stage2_summary.json", summary)
    report_writer(input_dir / "stage2_report.md", summary)
    atomic_write_json(input_dir / "schema_manifest.json", manifest)
    return summary


def _build_summary(state, hashes, input_counts) -> dict[str, object]:
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION,
        "mode": "offline_deterministic_alignment_streaming", "network_access": False,
        "commits": int(input_counts["commits.csv"]),
        "events": int(input_counts["events.csv"]),
        "episodes": int(state["episodes"]),
        "episode_event_links": int(state["episode_event_links"]),
        "quadrants": state.get("quadrants", {}),
        "alignment_statuses": state.get("alignment_statuses", {}),
        "query_kinds": state.get("query_kinds", {}),
        "stage1_table_sha256": hashes, "state_file": "stage2_state.json",
        "invariants": {
            "stage1_tables_unchanged": True,
            "four_table_manifest_complete": True, "network_access": False,
        },
    }


def _store(input_dir, resume, hashes, input_counts, context_signature) -> CheckpointedCsvStore:
    targets = {
        "episodes": _target("episodes.csv", "episodes"),
        "links": _target("episode_event_links.csv", "episode_event_links"),
    }
    seed = {
        "stage": "rq2_stage2", "stage1_table_sha256": hashes,
        "stage1_row_counts": input_counts, "context_signature": context_signature,
    }
    return CheckpointedCsvStore(
        input_dir, targets, "stage2_state.json", resume=resume, seed=seed
    )


def _target(filename: str, count_key: str) -> CsvTarget:
    return CsvTarget(filename, table_contract(filename).fields, count_key)


def _summary_counters(state) -> dict[str, Counter]:
    return {
        "quadrants": _counter(state, "quadrants"),
        "alignment": _counter(state, "alignment_statuses"),
        "query_kinds": _counter(state, "query_kinds"),
    }


def _counter(state: Mapping[str, object], key: str) -> Counter:
    value = state.get(key, {})
    return Counter(value if isinstance(value, Mapping) else {})


def _plain(counter: Counter) -> dict[str, int]:
    return dict(sorted(counter.items()))


def _prepare_inputs(input_dir: Path, resume: bool) -> None:
    required = [input_dir / name for name in ("commits.csv", "events.csv", "schema_manifest.json")]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing Stage 1 inputs: {missing}")
    targets = ("episodes.csv", "episode_event_links.csv", "stage2_state.json", *FINAL_OUTPUTS)
    existing = [name for name in targets if (input_dir / name).exists()]
    if existing and not resume:
        raise FileExistsError(f"refusing to overwrite Stage 2 outputs: {existing}")


def _stage1_hashes(input_dir: Path) -> dict[str, str]:
    return {name: _sha256(input_dir / name) for name in ("commits.csv", "events.csv")}


def _stage1_counts(input_dir: Path) -> dict[str, int]:
    manifest_path = input_dir / "schema_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tables = manifest.get("tables", {})
    try:
        return {
            name: int(tables[name]["row_count"])
            for name in ("commits.csv", "events.csv")
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid Stage 1 row counts in schema_manifest.json") from exc

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_complete_summary(input_dir: Path) -> dict[str, object]:
    path = input_dir / "stage2_summary.json"
    if not path.is_file():
        raise FileNotFoundError(f"complete Stage 2 state missing summary: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _cleanup_final_outputs(input_dir: Path) -> None:
    for name in FINAL_OUTPUTS:
        (input_dir / name).unlink(missing_ok=True)

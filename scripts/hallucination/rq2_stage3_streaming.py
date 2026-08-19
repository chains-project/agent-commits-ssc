"""Bounded-memory offline evidence application for RQ2 v2 Stage 3.

[IN]: Four canonical CSVs, full-record evidence JSONL, and Stage 3 policy callbacks.
[OUT]: Streamed episodes replacement, compact evidence/cache, checkpoint, and report.
[POS]: Stage 3 I/O boundary; policy stays in the existing registry/advisory modules.
[SYNC]: Keep rq2_stage3_apply.py, Stage 3 tests, scripts/CLAUDE.md, and
scripts/OUTPUTS.md synchronized with transaction or checkpoint changes.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping

from rq2_stage3_advisory_evidence import (
    EvidenceBundle,
    EvidenceItem,
    build_evidence_bundle,
)
from rq2_stage3_version_query import PACKAGING_VERSION
from rq2_stage_schema import SCHEMA_VERSION, table_contract, validate_row
from rq2_streaming_store import (
    CheckpointedCsvStore,
    CsvTarget,
    atomic_write_json,
    iter_csv_rows,
    read_json,
)


WORK_NAME = ".stage3_apply_work"
STATE_NAME = "stage3_state.json"
FINAL_OUTPUTS = (
    "evidence.jsonl", "stage3_evidence_cache",
    "stage3_summary.json", "stage3_report.md", STATE_NAME,
)
EVIDENCE_FIELDS = (
    "evidence_id", "evidence_type", "query_key", "source", "lookup_status",
    "queried_at", "payload_sha256", "payload_path", "error", "schema_version",
)


@dataclass(frozen=True)
class EvidenceLocator:
    query_key: str
    evidence_id: str
    payload_sha256: str
    offset: int
    length: int


class LazyEvidenceMapping(Mapping[str, EvidenceItem]):
    def __init__(self, index: "EvidenceOffsetIndex") -> None:
        self.index = index

    def __getitem__(self, key: str) -> EvidenceItem:
        return self.index.by_query(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.index.query_locators)

    def __len__(self) -> int:
        return len(self.index.query_locators)


class EvidenceOffsetIndex:
    def __init__(self, path: Path, cache_size: int = 1024) -> None:
        self.path = Path(path)
        self.cache_size = cache_size
        self.query_locators: dict[str, EvidenceLocator] = {}
        self.id_locators: dict[str, EvidenceLocator] = {}
        self.cache: OrderedDict[str, EvidenceItem] = OrderedDict()
        self.sha256 = self._scan()

    @property
    def bundle(self) -> EvidenceBundle:
        return EvidenceBundle(LazyEvidenceMapping(self), (), {})

    def by_query(self, query_key: str) -> EvidenceItem:
        try:
            locator = self.query_locators[query_key]
        except KeyError as exc:
            raise KeyError(query_key) from exc
        return self._load(locator)

    def by_id(self, evidence_id: str) -> EvidenceItem:
        try:
            locator = self.id_locators[evidence_id]
        except KeyError as exc:
            raise ValueError(f"unknown used evidence id: {evidence_id}") from exc
        return self._load(locator)

    def _scan(self) -> str:
        digest = hashlib.sha256()
        with self.path.open("rb") as handle:
            while True:
                offset, raw = handle.tell(), handle.readline()
                if not raw:
                    break
                digest.update(raw)
                if raw.strip():
                    self._index_line(raw, offset)
        return digest.hexdigest()

    def _index_line(self, raw: bytes, offset: int) -> None:
        try:
            record = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid evidence JSONL at byte {offset}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"evidence at byte {offset} must be an object")
        item = build_evidence_bundle([record]).items[0]
        locator = EvidenceLocator(
            item.query_key, item.evidence_id, item.payload_sha256, offset, len(raw)
        )
        previous = self.query_locators.get(item.query_key)
        if previous and previous.payload_sha256 != item.payload_sha256:
            raise ValueError(f"conflicting evidence for query: {item.query_key}")
        self.query_locators[item.query_key] = locator
        self.id_locators[item.evidence_id] = locator

    def _load(self, locator: EvidenceLocator) -> EvidenceItem:
        cached = self.cache.get(locator.evidence_id)
        if cached is not None:
            self.cache.move_to_end(locator.evidence_id)
            return cached
        with self.path.open("rb") as handle:
            handle.seek(locator.offset)
            raw = handle.read(locator.length)
        record = json.loads(raw.decode("utf-8-sig"))
        item = build_evidence_bundle([record]).items[0]
        if item.evidence_id != locator.evidence_id:
            raise ValueError("evidence input changed after offset indexing")
        self._remember(item)
        return item

    def _remember(self, item: EvidenceItem) -> None:
        self.cache[item.evidence_id] = item
        self.cache.move_to_end(item.evidence_id)
        while len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)


def run_stage3_streaming(
    input_dir: Path, evidence_input: Path, enrich_rows: Callable,
    report_writer: Callable, manifest_builder: Callable, *,
    resume: bool = False, checkpoint_every: int = 1000,
    max_buffer_rows: int = 10_000,
) -> dict[str, object]:
    status = _prepare_run(input_dir, evidence_input, resume)
    if status == "complete":
        return read_json(input_dir / "stage3_summary.json")
    protected = _protected_hashes(input_dir)
    episode_hash = _sha256(input_dir / "episodes.csv")
    evidence = EvidenceOffsetIndex(evidence_input)
    authors = _author_dates(input_dir / "commits.csv")
    work = input_dir / WORK_NAME
    store = _store(work, resume, episode_hash, evidence.sha256)
    state = store.prepare()
    try:
        return _run_incomplete(
            input_dir, evidence_input, evidence, authors, protected,
            enrich_rows, report_writer, manifest_builder, store, state,
            checkpoint_every, max_buffer_rows,
        )
    except Exception:
        _handle_failure(input_dir, work, store, resume)
        raise


def _run_incomplete(
    input_dir, evidence_input, evidence, authors, protected,
    enrich_rows, report_writer, manifest_builder, store, state,
    checkpoint_every, max_buffer_rows,
):
    batch, used = [], set(state.get("used_evidence_ids", []))
    counters = _status_counters(state)
    bundle = evidence.bundle
    processed, checkpointed = int(state["processed_groups"]), int(state["processed_groups"])
    fields = table_contract("episodes.csv").fields
    for index, episode in enumerate(
        iter_csv_rows(input_dir / "episodes.csv", fields), start=1
    ):
        if index <= processed:
            continue
        row, row_used = _enrich_one(episode, authors, bundle, enrich_rows)
        batch.append(row)
        used.update(row_used)
        _update_statuses(counters, row)
        processed = index
        if _checkpoint_due(processed, checkpointed, len(batch), checkpoint_every, max_buffer_rows):
            state = _commit(store, processed, batch, used, counters)
            batch, checkpointed = [], processed
    state = _commit(store, processed, batch, used, counters, data_complete=True)
    return _finalize(
        input_dir, evidence_input, evidence, authors, protected,
        report_writer, manifest_builder, store, state,
    )


def _enrich_one(episode, authors, bundle, enrich_rows):
    author_date = authors.get(episode["commit_id"])
    if author_date is None:
        raise ValueError(f"episode references unknown commit: {episode['episode_id']}")
    commits = [{"commit_id": episode["commit_id"], "author_date": author_date}]
    updated, used = enrich_rows([episode], commits, bundle)
    row = updated[0]
    validate_row("episodes.csv", row)
    return row, used


def _checkpoint_due(processed, checkpointed, buffered, group_limit, row_limit) -> bool:
    by_groups = group_limit > 0 and processed - checkpointed >= group_limit
    by_rows = row_limit > 0 and buffered >= row_limit
    return by_groups or by_rows


def _commit(store, processed, batch, used, counters, data_complete=False):
    extra = {
        "used_evidence_ids": sorted(used),
        "registry_statuses": dict(sorted(counters["registry"].items())),
        "advisory_statuses": dict(sorted(counters["advisory"].items())),
    }
    return store.commit(
        processed, {"episodes": batch}, extra=extra, data_complete=data_complete
    )


def _finalize(
    input_dir, evidence_input, evidence, authors, protected,
    report_writer, manifest_builder, store, state,
):
    work = input_dir / WORK_NAME
    _verify_sources(input_dir, evidence_input, evidence, state)
    _clear_staged_final_outputs(work)
    evidence_records, payloads = _write_used_evidence(
        work, evidence, set(state.get("used_evidence_ids", []))
    )
    summary = _summary(
        input_dir, work, state, authors, protected, evidence_records, payloads,
    )
    atomic_write_json(work / "stage3_summary.json", summary)
    report_writer(work / "stage3_report.md", summary)
    _copy_rollback_inputs(input_dir, work)
    committed = False
    try:
        _commit_staging(input_dir, work)
        committed = True
        atomic_write_json(input_dir / "schema_manifest.json", manifest_builder(input_dir))
        _verify_protected(input_dir, protected)
        store.mark_complete()
        os.replace(work / STATE_NAME, input_dir / STATE_NAME)
        _discard_rollback(work)
        return summary
    except Exception:
        _rollback_outputs(input_dir, work, committed)
        raise


def _verify_sources(input_dir, evidence_input, evidence, state) -> None:
    episode_hash = _sha256(input_dir / "episodes.csv")
    if episode_hash != state["stage2_episode_sha256"]:
        raise RuntimeError("Stage 3 source episodes changed during processing")
    if _sha256(evidence_input) != evidence.sha256:
        raise RuntimeError("Stage 3 evidence input changed during processing")

def _summary(
    input_dir, work, state, authors, protected, evidence_records, payloads,
) -> dict[str, object]:
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION, "mode": "offline_fixed_evidence_streaming",
        "network_access": False, "packaging_version": PACKAGING_VERSION,
        "commits": len(authors), "episodes": int(state["episodes"]),
        "evidence_records": evidence_records, "evidence_payloads": payloads,
        "evidence_input_sha256": state["evidence_input_sha256"],
        "registry_statuses": state.get("registry_statuses", {}),
        "advisory_statuses": state.get("advisory_statuses", {}),
        "stage2_episode_sha256": state["stage2_episode_sha256"],
        "stage3_episode_sha256": _sha256(work / "episodes.csv"),
        "protected_table_sha256": protected, "state_file": STATE_NAME,
        "invariants": {
            "network_access": False, "four_canonical_tables_only": True,
            "protected_tables_unchanged": True,
            "all_queryable_episodes_processed": True,
        },
    }


def _write_used_evidence(work, evidence, used_ids) -> tuple[int, int]:
    cache = work / "stage3_evidence_cache"
    cache.mkdir(parents=True, exist_ok=True)
    payloads = set()
    with (work / "evidence.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for evidence_id in sorted(used_ids):
            item = evidence.by_id(evidence_id)
            if tuple(item.metadata) != EVIDENCE_FIELDS:
                raise ValueError("invalid evidence metadata fields")
            handle.write(json.dumps(item.metadata, ensure_ascii=False, separators=(",", ":")) + "\n")
            _write_payload(work, item)
            payloads.add(item.payload_sha256)
        handle.flush()
        os.fsync(handle.fileno())
    return len(used_ids), len(payloads)


def _write_payload(work: Path, item: EvidenceItem) -> None:
    path = work / str(item.metadata["payload_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != item.payload:
        raise ValueError(f"payload hash collision: {item.payload_sha256}")
    if not path.exists():
        path.write_bytes(item.payload)


def _store(work, resume, episode_hash, evidence_hash) -> CheckpointedCsvStore:
    target = CsvTarget(
        "episodes.csv", table_contract("episodes.csv").fields, "episodes"
    )
    seed = {
        "stage": "rq2_stage3", "stage2_episode_sha256": episode_hash,
        "evidence_input_sha256": evidence_hash,
    }
    return CheckpointedCsvStore(
        work, {"episodes": target}, STATE_NAME, resume=resume, seed=seed
    )


def _prepare_run(input_dir: Path, evidence_input: Path, resume: bool) -> str:
    required = [input_dir / name for name in _table_names()]
    required.append(input_dir / "schema_manifest.json")
    missing = [str(path) for path in required if not path.is_file()]
    if not evidence_input.is_file():
        missing.append(str(evidence_input))
    if missing:
        raise FileNotFoundError(f"missing Stage 3 inputs: {missing}")
    if resume and _completed(input_dir):
        return "complete"
    existing = [name for name in FINAL_OUTPUTS if (input_dir / name).exists()]
    work = input_dir / WORK_NAME
    if existing or (work.exists() and not resume):
        raise FileExistsError(f"refusing to overwrite Stage 3 outputs: {existing or [WORK_NAME]}")
    work.mkdir(exist_ok=resume)
    return "run"


def _completed(input_dir: Path) -> bool:
    state_path = input_dir / STATE_NAME
    if not state_path.is_file():
        return False
    state = read_json(state_path)
    if state.get("complete") is not True:
        return False
    _verify_completed_outputs(input_dir, state)
    return True


def _verify_completed_outputs(input_dir: Path, state: Mapping[str, object]) -> None:
    missing = [name for name in FINAL_OUTPUTS if not (input_dir / name).exists()]
    if missing:
        raise RuntimeError(f"completed Stage 3 outputs missing: {missing}")
    summary = read_json(input_dir / "stage3_summary.json")
    episode_path = input_dir / "episodes.csv"
    offsets = state.get("csv_bytes", {})
    expected_bytes = int(offsets.get("episodes", -1)) if isinstance(offsets, Mapping) else -1
    if episode_path.stat().st_size != expected_bytes:
        raise RuntimeError("completed Stage 3 episodes byte size mismatch")
    episode_hash = _sha256(episode_path)
    if episode_hash != summary.get("stage3_episode_sha256"):
        raise RuntimeError("completed Stage 3 episodes SHA-256 mismatch")
    _verify_completed_manifest(input_dir, state, summary, episode_hash)


def _verify_completed_manifest(input_dir, state, summary, episode_hash) -> None:
    manifest = read_json(input_dir / "schema_manifest.json")
    tables = manifest.get("tables", {})
    episode = tables.get("episodes.csv", {}) if isinstance(tables, Mapping) else {}
    if episode.get("sha256") != episode_hash:
        raise RuntimeError("completed Stage 3 episodes manifest SHA-256 mismatch")
    counts = (state.get("episodes"), summary.get("episodes"), episode.get("row_count"))
    if len({int(value) for value in counts}) != 1:
        raise RuntimeError("completed Stage 3 episodes row count mismatch")


def _table_names() -> tuple[str, ...]:
    return ("commits.csv", "events.csv", "episodes.csv", "episode_event_links.csv")


def _author_dates(path: Path) -> dict[str, str]:
    authors = {}
    for row in iter_csv_rows(path, table_contract("commits.csv").fields):
        validate_row("commits.csv", row)
        if row["commit_id"] in authors:
            raise ValueError(f"duplicate commit_id: {row['commit_id']}")
        authors[row["commit_id"]] = row["author_date"]
    return authors


def _status_counters(state) -> dict[str, Counter]:
    return {
        "registry": Counter(state.get("registry_statuses", {})),
        "advisory": Counter(state.get("advisory_statuses", {})),
    }


def _update_statuses(counters, row) -> None:
    counters["registry"][row["registry_status"]] += 1
    counters["advisory"][row["advisory_status"]] += 1


def _protected_hashes(input_dir: Path) -> dict[str, str]:
    names = ("commits.csv", "events.csv", "episode_event_links.csv")
    return {name: _sha256(input_dir / name) for name in names}


def _verify_protected(input_dir: Path, expected: Mapping[str, str]) -> None:
    if _protected_hashes(input_dir) != dict(expected):
        raise RuntimeError("Stage 3 modified a protected canonical table")


def _copy_rollback_inputs(input_dir: Path, work: Path) -> None:
    shutil.copyfile(input_dir / "episodes.csv", work / "episodes.original.csv")
    shutil.copyfile(
        input_dir / "schema_manifest.json", work / "schema_manifest.original.json"
    )


def _commit_staging(input_dir: Path, work: Path) -> None:
    for name in FINAL_OUTPUTS[:-1]:
        os.replace(work / name, input_dir / name)
    os.replace(work / "episodes.csv", input_dir / "episodes.csv")


def _rollback_outputs(input_dir: Path, work: Path, committed: bool) -> None:
    if committed and (work / "episodes.original.csv").is_file():
        os.replace(work / "episodes.original.csv", input_dir / "episodes.csv")
        os.replace(
            work / "schema_manifest.original.json", input_dir / "schema_manifest.json"
        )
    for name in FINAL_OUTPUTS:
        _remove_path(input_dir / name)
    if work.exists():
        shutil.rmtree(work)


def _discard_rollback(work: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)


def _handle_failure(input_dir, work, store, resume) -> None:
    if not work.exists():
        return
    if resume:
        return
    store.abort_fresh()
    shutil.rmtree(work, ignore_errors=True)


def _clear_staged_final_outputs(work: Path) -> None:
    for name in FINAL_OUTPUTS[:-1]:
        _remove_path(work / name)


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

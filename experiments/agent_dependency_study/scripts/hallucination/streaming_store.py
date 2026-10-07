"""Bounded-memory CSV grouping and byte-offset checkpoint primitives for RQ2.

[IN]: Versioned CSV targets, ordered canonical inputs, and a stage state seed.
[OUT]: Append-only CSV groups, atomic JSON state, deterministic incomplete-resume
truncation, and non-mutating completed-resume checks.
[POS]: Shared local I/O boundary for RQ2 v2 Stage 1-3; no network access.
[SYNC]: Keep Stage 1-3 runners, streaming tests, scripts/CLAUDE.md, and
scripts/OUTPUTS.md synchronized with state or transaction changes.
"""

from __future__ import annotations

import csv
import itertools
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence


STATE_SCHEMA_VERSION = 1
REPLACE_ATTEMPTS = 10
REPLACE_RETRY_BASE_SECONDS = 0.05
REPLACE_RETRY_MAX_SECONDS = 0.8


@dataclass(frozen=True)
class CsvTarget:
    filename: str
    fields: tuple[str, ...]
    count_key: str


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    _replace_with_retry(temporary, path)


def _replace_with_retry(source: Path, destination: Path) -> None:
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt + 1 == REPLACE_ATTEMPTS:
                raise
            delay = min(
                REPLACE_RETRY_BASE_SECONDS * (2 ** attempt),
                REPLACE_RETRY_MAX_SECONDS,
            )
            time.sleep(delay)


def read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"invalid checkpoint: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"checkpoint must be an object: {path}")
    return value


def file_signature(path: Path) -> dict[str, object]:
    resolved = path.resolve()
    stat = resolved.stat()
    return {
        "path": str(resolved), "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def iter_csv_rows(
    path: Path, expected_fields: Sequence[str] | None = None
) -> Iterator[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(line.replace("\x00", "") for line in handle)
        if expected_fields and tuple(reader.fieldnames or ()) != tuple(expected_fields):
            raise ValueError(f"invalid header for {path.name}")
        yield from reader


class OrderedCsvGroupCursor:
    def __init__(
        self, path: Path, key_fields: tuple[str, ...],
        expected_fields: Sequence[str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.key_fields = key_fields
        self.expected_fields = expected_fields
        self.handle = None
        self.groups = None
        self.current_key: tuple[str, ...] | None = None
        self.current_rows: list[dict[str, str]] = []

    def __enter__(self) -> "OrderedCsvGroupCursor":
        self.handle = self.path.open(
            "r", encoding="utf-8-sig", errors="replace", newline=""
        )
        reader = csv.DictReader(line.replace("\x00", "") for line in self.handle)
        if self.expected_fields and tuple(reader.fieldnames or ()) != tuple(self.expected_fields):
            raise ValueError(f"invalid header for {self.path.name}")
        self.groups = itertools.groupby(reader, key=self._key)
        self._advance()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        if self.handle is not None:
            self.handle.close()

    @property
    def exhausted(self) -> bool:
        return self.current_key is None

    def take(self, expected_key: tuple[str, ...]) -> list[dict[str, str]]:
        if self.current_key != expected_key:
            return []
        rows = self.current_rows
        self._advance()
        return rows

    def _key(self, row: Mapping[str, str]) -> tuple[str, ...]:
        return tuple(row.get(field, "") for field in self.key_fields)

    def _advance(self) -> None:
        try:
            key, rows = next(self.groups)
        except StopIteration:
            self.current_key, self.current_rows = None, []
            return
        self.current_key, self.current_rows = key, list(rows)


class CheckpointedCsvStore:
    def __init__(
        self, output_dir: Path, targets: Mapping[str, CsvTarget],
        state_name: str, *, resume: bool, seed: Mapping[str, object],
    ) -> None:
        self.output_dir = Path(output_dir)
        self.targets = dict(targets)
        self.state_path = self.output_dir / state_name
        self.resume = resume
        self.seed = dict(seed)
        self.state: dict[str, object] = {}
        self.created_fresh = False

    def prepare(self) -> dict[str, object]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        existing = self._existing_targets()
        if self.resume and self.state_path.exists():
            self.state = read_json(self.state_path)
            self._validate_state()
            if self.state.get("complete") is True:
                return dict(self.state)
            self._truncate_to_checkpoint()
        elif existing:
            raise FileExistsError(f"refusing uncheckpointed outputs: {existing}")
        else:
            self.created_fresh = True
            self.state = self._new_state()
            self._write_headers()
        self._record_offsets()
        atomic_write_json(self.state_path, self.state)
        return dict(self.state)

    def commit(
        self, processed_groups: int, batches: Mapping[str, list[dict[str, str]]],
        *, extra: Mapping[str, object] | None = None, data_complete: bool = False,
    ) -> dict[str, object]:
        self._validate_batches(batches)
        for name, target in self.targets.items():
            count = _append_rows(
                self.output_dir / target.filename, target.fields, batches.get(name, [])
            )
            self.state[target.count_key] = int(self.state[target.count_key]) + count
        self.state["processed_groups"] = processed_groups
        self.state["data_complete"] = data_complete
        if extra:
            self.state.update(extra)
        self._record_offsets()
        atomic_write_json(self.state_path, self.state)
        return dict(self.state)

    def mark_complete(self, extra: Mapping[str, object] | None = None) -> dict[str, object]:
        self.state["complete"] = True
        if extra:
            self.state.update(extra)
        atomic_write_json(self.state_path, self.state)
        return dict(self.state)

    def abort_fresh(self) -> None:
        if not self.created_fresh:
            return
        for target in self.targets.values():
            (self.output_dir / target.filename).unlink(missing_ok=True)
        self.state_path.unlink(missing_ok=True)

    def _new_state(self) -> dict[str, object]:
        state = {
            "state_schema_version": STATE_SCHEMA_VERSION,
            "processed_groups": 0, "data_complete": False, "complete": False,
            **self.seed,
        }
        for target in self.targets.values():
            state[target.count_key] = 0
        return state

    def _existing_targets(self) -> list[str]:
        names = [target.filename for target in self.targets.values()]
        return [name for name in (*names, self.state_path.name) if (self.output_dir / name).exists()]

    def _write_headers(self) -> None:
        for target in self.targets.values():
            path = self.output_dir / target.filename
            with path.open("w", encoding="utf-8", newline="") as handle:
                csv.DictWriter(handle, fieldnames=target.fields).writeheader()
                handle.flush()
                os.fsync(handle.fileno())

    def _validate_state(self) -> None:
        if self.state.get("state_schema_version") != STATE_SCHEMA_VERSION:
            raise ValueError("unsupported checkpoint schema")
        for key, value in self.seed.items():
            if self.state.get(key) != value:
                raise ValueError(f"checkpoint seed mismatch: {key}")

    def _truncate_to_checkpoint(self) -> None:
        offsets = self.state.get("csv_bytes")
        if not isinstance(offsets, dict):
            raise ValueError("checkpoint missing csv_bytes")
        for name, target in self.targets.items():
            _truncate(self.output_dir / target.filename, int(offsets[name]))

    def _record_offsets(self) -> None:
        self.state["csv_bytes"] = {
            name: (self.output_dir / target.filename).stat().st_size
            for name, target in self.targets.items()
        }

    def _validate_batches(self, batches: Mapping[str, object]) -> None:
        unknown = set(batches) - set(self.targets)
        if unknown:
            raise ValueError(f"unknown CSV batches: {sorted(unknown)}")


def _append_rows(path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, str]]) -> int:
    materialized = list(rows)
    if not materialized:
        return 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=fields).writerows(materialized)
        handle.flush()
        os.fsync(handle.fileno())
    return len(materialized)


def _truncate(path: Path, expected: int) -> None:
    if not path.exists():
        raise FileNotFoundError(f"checkpoint CSV missing: {path}")
    actual = path.stat().st_size
    if actual < expected:
        raise ValueError(f"CSV shorter than checkpoint: {path}")
    if actual > expected:
        with path.open("r+b") as handle:
            handle.truncate(expected)
            handle.flush()
            os.fsync(handle.fileno())

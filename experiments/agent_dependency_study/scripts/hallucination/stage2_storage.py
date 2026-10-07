"""Bounded-memory CSV/checkpoint primitives for hallucination Stage2.

[IN]: Stage2 CSV schemas, output directory, and stage2_checkpoint.json.
[OUT]: append-only committed CSV batches, byte-offset checkpoints, and atomic
derived CSV replacements.
[POS]: Storage boundary used by manifest_enrich.py to avoid retaining or
rewriting multi-gigabyte dependency tables in memory.
[SYNC]: If checkpoint keys or CSV transaction semantics change, update
scripts/CLAUDE.md and scripts/OUTPUTS.md.
"""

from __future__ import annotations

import csv
import itertools
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence


@dataclass(frozen=True)
class CsvSpec:
    filename: str
    fields: tuple[str, ...]
    count_key: str


def write_header(path: Path, fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=fields).writeheader()
        handle.flush()
        os.fsync(handle.fileno())


def append_rows(path: Path, fields: Sequence[str], rows: list[dict[str, str]]) -> int:
    if not rows:
        return 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    return len(rows)


def atomic_write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"invalid Stage2 checkpoint: {path}: {exc}") from exc


class Stage2AppendStore:
    def __init__(
        self,
        output_dir: Path,
        specs: Mapping[str, CsvSpec],
        *,
        resume: bool,
        checkpoint_name: str = "stage2_checkpoint.json",
    ) -> None:
        self.output_dir = Path(output_dir)
        self.specs = dict(specs)
        self.resume = resume
        self.checkpoint_path = self.output_dir / checkpoint_name
        self.state: dict[str, object] = {}

    def prepare(self) -> dict[str, object]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.resume and self.checkpoint_path.exists():
            self.state = read_json(self.checkpoint_path)
            self._prepare_resume_files()
        else:
            self.state = self._new_state()
            self._reset_files()
        self._record_byte_offsets()
        atomic_write_json(self.checkpoint_path, self.state)
        return dict(self.state)

    def commit(
        self,
        processed_commits: int,
        batches: Mapping[str, list[dict[str, str]]],
        *,
        fetch_complete: bool = False,
        complete: bool = False,
    ) -> dict[str, object]:
        for name, spec in self.specs.items():
            count = append_rows(
                self.output_dir / spec.filename,
                spec.fields,
                batches.get(name, []),
            )
            self.state[spec.count_key] = int(self.state.get(spec.count_key, 0)) + count
        self.state["processed_candidate_commits"] = processed_commits
        self.state["fetch_complete"] = fetch_complete
        self.state["complete"] = complete
        self.state["generated_utc"] = datetime.now(timezone.utc).isoformat()
        self._record_byte_offsets()
        atomic_write_json(self.checkpoint_path, self.state)
        return dict(self.state)

    def mark_complete(self) -> dict[str, object]:
        self.state["complete"] = True
        self.state["fetch_complete"] = True
        self.state["generated_utc"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(self.checkpoint_path, self.state)
        return dict(self.state)

    def _new_state(self) -> dict[str, object]:
        state: dict[str, object] = {
            "processed_candidate_commits": 0,
            "fetch_complete": False,
            "complete": False,
        }
        for spec in self.specs.values():
            state[spec.count_key] = 0
        return state

    def _reset_files(self) -> None:
        for spec in self.specs.values():
            write_header(self.output_dir / spec.filename, spec.fields)

    def _prepare_resume_files(self) -> None:
        offsets = self.state.get("csv_bytes")
        for name, spec in self.specs.items():
            path = self.output_dir / spec.filename
            expected_rows = int(self.state.get(spec.count_key, 0) or 0)
            if not path.exists():
                if expected_rows:
                    raise ValueError(f"checkpoint expects rows but CSV is missing: {path}")
                write_header(path, spec.fields)
            if isinstance(offsets, dict) and name in offsets:
                self._truncate_to_offset(path, int(offsets[name]))

    @staticmethod
    def _truncate_to_offset(path: Path, expected: int) -> None:
        actual = path.stat().st_size
        if actual < expected:
            raise ValueError(
                f"CSV shorter than checkpoint offset: {path} actual={actual} expected={expected}"
            )
        if actual > expected:
            with path.open("r+b") as handle:
                handle.truncate(expected)
                handle.flush()
                os.fsync(handle.fileno())

    def _record_byte_offsets(self) -> None:
        self.state["csv_bytes"] = {
            name: (self.output_dir / spec.filename).stat().st_size
            for name, spec in self.specs.items()
        }


class OrderedCsvGroupCursor:
    def __init__(self, path: Path, key_fields: tuple[str, ...]) -> None:
        self.path = Path(path)
        self.key_fields = key_fields
        self.handle = None
        self.groups = None
        self.current_key: tuple[str, ...] | None = None
        self.current_rows: list[dict[str, str]] = []

    def __enter__(self) -> "OrderedCsvGroupCursor":
        if not self.path.exists():
            return self
        self.handle = self.path.open(
            "r", encoding="utf-8-sig", errors="replace", newline=""
        )
        reader = csv.DictReader(line.replace("\x00", "") for line in self.handle)
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

    def _key(self, row: dict[str, str]) -> tuple[str, ...]:
        return tuple(row.get(field, "") for field in self.key_fields)

    def _advance(self) -> None:
        if self.groups is None:
            self.current_key, self.current_rows = None, []
            return
        try:
            key, rows = next(self.groups)
        except StopIteration:
            self.current_key, self.current_rows = None, []
            return
        self.current_key, self.current_rows = key, list(rows)


class AtomicCsvOutputs:
    def __init__(self, output_dir: Path, specs: Mapping[str, CsvSpec]) -> None:
        self.output_dir = Path(output_dir)
        self.specs = dict(specs)
        self.handles: dict[str, object] = {}
        self.writers: dict[str, csv.DictWriter] = {}
        self.temporary_paths: dict[str, Path] = {}
        self.counts = {name: 0 for name in self.specs}

    def __enter__(self) -> "AtomicCsvOutputs":
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for name, spec in self.specs.items():
            path = self.output_dir / f".{spec.filename}.{os.getpid()}.tmp"
            handle = path.open("w", encoding="utf-8", newline="")
            writer = csv.DictWriter(handle, fieldnames=spec.fields)
            writer.writeheader()
            self.handles[name] = handle
            self.writers[name] = writer
            self.temporary_paths[name] = path
        return self

    def write(self, name: str, rows: Iterable[dict[str, str]]) -> None:
        materialized = list(rows)
        if not materialized:
            return
        self.writers[name].writerows(materialized)
        self.counts[name] += len(materialized)

    def __exit__(self, error_type, _value, _traceback) -> None:
        self._close_handles()
        if error_type is None:
            for name, spec in self.specs.items():
                os.replace(
                    self.temporary_paths[name], self.output_dir / spec.filename
                )
        else:
            for path in self.temporary_paths.values():
                path.unlink(missing_ok=True)

    def _close_handles(self) -> None:
        for handle in self.handles.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()


def iter_csv_rows(path: Path) -> Iterator[dict[str, str]]:
    with Path(path).open(
        'r', encoding='utf-8-sig', errors='replace', newline=''
    ) as handle:
        reader = csv.DictReader(line.replace('\x00', '') for line in handle)
        yield from reader


def empty_batches(specs: Mapping[str, CsvSpec]) -> dict[str, list[dict[str, str]]]:
    return {name: [] for name in specs}


def extend_batches(
    batches: dict[str, list[dict[str, str]]],
    produced: Mapping[str, list[dict[str, str]]],
) -> None:
    unknown = set(produced) - set(batches)
    if unknown:
        raise ValueError(f'processor returned unknown CSV batches: {sorted(unknown)}')
    for name, rows in produced.items():
        batches[name].extend(rows)


def buffered_rows(batches: Mapping[str, list[dict[str, str]]]) -> int:
    return sum(len(rows) for rows in batches.values())


def stream_candidate_batches(
    candidate_path: Path,
    store: Stage2AppendStore,
    processor: Callable[
        [dict[str, str]], Mapping[str, list[dict[str, str]]]
    ],
    *,
    max_commits: int = 0,
    checkpoint_every: int = 100,
    max_buffer_rows: int = 100_000,
) -> dict[str, object]:
    state = store.prepare()
    if state.get('complete') or state.get('fetch_complete'):
        return state
    resume_after = int(state.get('processed_candidate_commits', 0) or 0)
    processed = resume_after
    last_commit = resume_after
    batches = empty_batches(store.specs)
    for index, candidate in enumerate(iter_csv_rows(candidate_path)):
        if max_commits and index >= max_commits:
            break
        if index < resume_after:
            continue
        extend_batches(batches, processor(candidate))
        processed = index + 1
        due_by_commit = checkpoint_every > 0 and processed - last_commit >= checkpoint_every
        due_by_rows = max_buffer_rows > 0 and buffered_rows(batches) >= max_buffer_rows
        if due_by_commit or due_by_rows:
            state = store.commit(processed, batches)
            batches = empty_batches(store.specs)
            last_commit = processed
    return store.commit(processed, batches, fetch_complete=True)


def stream_grouped_outputs(
    candidate_path: Path,
    import_path: Path,
    dependency_path: Path,
    output_dir: Path,
    specs: Mapping[str, CsvSpec],
    processor: Callable[
        [dict[str, str], list[dict[str, str]], list[dict[str, str]]],
        Mapping[str, list[dict[str, str]]],
    ],
    *,
    max_commits: int = 0,
) -> dict[str, int]:
    key_fields = ('repo', 'sha')
    with OrderedCsvGroupCursor(import_path, key_fields) as imports:
        with OrderedCsvGroupCursor(dependency_path, key_fields) as dependencies:
            with AtomicCsvOutputs(output_dir, specs) as outputs:
                for index, candidate in enumerate(iter_csv_rows(candidate_path)):
                    if max_commits and index >= max_commits:
                        break
                    key = tuple(candidate.get(field, '') for field in key_fields)
                    produced = processor(
                        candidate, imports.take(key), dependencies.take(key)
                    )
                    for name, rows in produced.items():
                        if name not in specs:
                            raise ValueError(f'processor returned unknown output: {name}')
                        outputs.write(name, rows)
                if not max_commits and (not imports.exhausted or not dependencies.exhausted):
                    raise ValueError('grouped Stage2 CSV order does not match candidates')
            return dict(outputs.counts)

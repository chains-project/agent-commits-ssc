"""Discover Stage 2 queries and collect recoverable RQ2 v2 Stage 3 evidence.

[IN]: Stage 2 episodes.csv, prior six-language results, read-only legacy
caches, and official registry/OSV APIs.
[OUT]: stage3_evidence_input.jsonl, collector SQLite checkpoint, summary, and
independent v2 caches.
[POS]: Networked Stage 3 collection entry point; apply remains offline and
does not increase the canonical CSV count.
[SYNC]: Keep Stage 3 apply, OUTPUTS.md, CLAUDE.md, and the design contract
aligned when query keys, checkpoints, or outputs change.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from rq2_stage3_advisory_evidence import advisory_query_key, build_evidence_bundle
from rq2_stage3_legacy_evidence import LegacyEvidenceIndex
from rq2_stage3_osv_collector import OsvCollector
from rq2_stage3_registry_collector import RegistryCollector
from rq2_stage3_version_query import registry_query_key
from rq2_streaming_store import atomic_write_json
from rq2_stage_schema import table_contract, validate_row


LANGUAGES = ("TypeScript", "Python", "JavaScript", "Rust", "Go", "Java")
DEFAULT_REGISTRY_ERROR_RATE_THRESHOLD = 0.02
DEFAULT_REGISTRY_ERROR_RATE_MIN_SAMPLES = 500
DEFAULT_DATA_ROOT = Path(os.environ.get("THESIS_DATA_ROOT", "data_external"))
DEFAULT_LEGACY_ROOT = Path("data_products/rq3_hallucinated_unlimited_overnight_20260710/wave01_nall")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_DATA_ROOT / "cache" / "rq2_stage3_v2")
    parser.add_argument("--legacy-cache-root", type=Path, default=DEFAULT_DATA_ROOT / "cache" / "rq3_hallucinated" / "registry")
    parser.add_argument("--legacy-root", type=Path, default=DEFAULT_LEGACY_ROOT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--retry-count", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=0.5)
    parser.add_argument("--registry-batch-size", type=int, default=50)
    parser.add_argument("--osv-batch-size", type=int, default=100)
    parser.add_argument("--maven-workers", type=int, default=1)
    parser.add_argument(
        "--registry-error-rate-threshold", type=float,
        default=DEFAULT_REGISTRY_ERROR_RATE_THRESHOLD,
    )
    parser.add_argument(
        "--registry-error-rate-min-samples", type=int,
        default=DEFAULT_REGISTRY_ERROR_RATE_MIN_SAMPLES,
    )
    parser.add_argument("--max-registry-queries", type=int, default=0)
    parser.add_argument("--max-advisory-queries", type=int, default=0)
    return parser.parse_args()


def run_collector(
    input_dir: Path, evidence_output: Path, state_db: Path, cache_root: Path,
    legacy_cache_root: Path, legacy_directories: Sequence[Path], *, resume: bool = False,
    timeout: int = 25, retry_count: int = 2, retry_sleep: float = 0.5,
    registry_batch_size: int = 50, osv_batch_size: int = 100,
    maven_workers: int = 1,
    registry_error_rate_threshold: float = DEFAULT_REGISTRY_ERROR_RATE_THRESHOLD,
    registry_error_rate_min_samples: int = DEFAULT_REGISTRY_ERROR_RATE_MIN_SAMPLES,
    max_registry_queries: int = 0, max_advisory_queries: int = 0,
) -> dict[str, object]:
    _prepare_paths(evidence_output, state_db, resume)
    connection = sqlite3.connect(state_db)
    try:
        collectors = _collectors(
            cache_root, legacy_cache_root, legacy_directories,
            timeout, retry_count, retry_sleep, maven_workers,
        )
        limits = (
            registry_batch_size, max_registry_queries,
            osv_batch_size, max_advisory_queries,
            registry_error_rate_min_samples, registry_error_rate_threshold,
        )
        return _run_collection(
            connection, input_dir, evidence_output, collectors, limits,
        )
    finally:
        connection.close()


def _run_collection(connection, input_dir, output, collectors, limits):
    _initialize(connection)
    _discover(connection, input_dir / "episodes.csv")
    _resume_output(connection, output)
    registry, osv = collectors
    registry_batch, registry_max, osv_batch, osv_max, minimum, threshold = limits
    try:
        reg_done = _collect_registry(
            connection, output, registry, registry_batch, registry_max,
            error_rate_min_samples=minimum, error_rate_threshold=threshold,
        )
    except CollectorQualityPause as pause:
        return _save_summary(
            connection, input_dir, output, False, {"pause": pause.details},
        )
    if not reg_done:
        return _save_summary(
            connection, input_dir, output, False,
            {"pause": {"reason": "registry_retry_pending"}},
        )
    adv_done = _collect_advisory(
        connection, output, osv, osv_batch, osv_max,
    )
    return _save_summary(connection, input_dir, output, adv_done)


def _collectors(
    cache_root, legacy_cache_root, directories, timeout, retries, sleep,
    maven_workers,
):
    legacy = LegacyEvidenceIndex.from_directories(directories)
    registry = RegistryCollector(
        cache_root, legacy_cache_root, legacy, timeout=timeout,
        retry_count=retries, retry_sleep=sleep, maven_workers=maven_workers,
    )
    osv = OsvCollector(
        cache_root, legacy, timeout=timeout,
        retry_count=retries, retry_sleep=sleep,
    )
    return registry, osv


def _prepare_paths(evidence: Path, database: Path, resume: bool) -> None:
    evidence.parent.mkdir(parents=True, exist_ok=True)
    database.parent.mkdir(parents=True, exist_ok=True)
    existing = [str(path) for path in (evidence, database) if path.exists()]
    if existing and not resume:
        raise FileExistsError(f"refusing to overwrite collector outputs: {existing}")


def _initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS query (
          query_key TEXT PRIMARY KEY, evidence_type TEXT NOT NULL,
          ecosystem TEXT NOT NULL, package_name TEXT NOT NULL,
          query_kind TEXT NOT NULL, query_value TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS registry_value (
          query_key TEXT NOT NULL, query_kind TEXT NOT NULL, query_value TEXT NOT NULL,
          PRIMARY KEY(query_key, query_kind, query_value));
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS query_attempt (
          query_key TEXT PRIMARY KEY, attempt_count INTEGER NOT NULL,
          last_error TEXT NOT NULL, last_attempted_utc TEXT NOT NULL);
        """
    )
    connection.execute("INSERT OR IGNORE INTO meta VALUES('committed_offset', '0')")
    connection.execute("UPDATE query SET status='pending' WHERE status='retry'")
    connection.commit()


def _discover(connection: sqlite3.Connection, path: Path) -> None:
    if _meta(connection, "discovery_complete") == "true":
        return
    fields = table_contract("episodes.csv").fields
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != fields:
            raise ValueError("invalid header for episodes.csv")
        for index, row in enumerate(reader, start=1):
            validate_row("episodes.csv", row)
            _insert_episode_queries(connection, row)
            if index % 10_000 == 0:
                connection.commit()
    _set_meta(connection, "discovery_complete", "true")
    connection.commit()


def _insert_episode_queries(connection: sqlite3.Connection, row: Mapping[str, str]) -> None:
    kind = row["query_kind"]
    if kind in {"excluded", "unresolved"}:
        return
    registry_key = registry_query_key(row["ecosystem"], row["package_name"])
    _insert_query(connection, registry_key, "registry", row, "", "")
    connection.execute(
        "INSERT OR IGNORE INTO registry_value VALUES(?,?,?)",
        (registry_key, kind, row["query_value"]),
    )
    advisory_key = advisory_query_key(
        row["ecosystem"], row["package_name"], kind, row["query_value"]
    )
    _insert_query(connection, advisory_key, "advisory", row, kind, row["query_value"])


def _insert_query(connection, key, evidence_type, row, kind, value) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO query(query_key,evidence_type,ecosystem,package_name,query_kind,query_value) VALUES(?,?,?,?,?,?)",
        (key, evidence_type, row["ecosystem"], row["package_name"], kind, value),
    )


def _resume_output(connection: sqlite3.Connection, path: Path) -> None:
    committed = int(_meta(connection, "committed_offset") or 0)
    if not path.exists():
        if committed:
            raise RuntimeError("collector evidence is missing but checkpoint offset is nonzero")
        path.touch()
        return
    size = path.stat().st_size
    if size < committed:
        raise RuntimeError("collector evidence is shorter than committed checkpoint")
    with path.open("r+b") as handle:
        handle.truncate(committed)


class CollectorQualityPause(RuntimeError):
    def __init__(self, details: dict[str, object]) -> None:
        super().__init__(str(details))
        self.details = details


def _collect_registry(
    connection, output, collector, batch_size, maximum, *,
    error_rate_min_samples=DEFAULT_REGISTRY_ERROR_RATE_MIN_SAMPLES,
    error_rate_threshold=DEFAULT_REGISTRY_ERROR_RATE_THRESHOLD,
) -> bool:
    processed = observed = errors = 0
    while not maximum or processed < maximum:
        limit = min(batch_size, maximum - processed) if maximum else batch_size
        rows = _pending(connection, "registry", limit)
        if not rows:
            return not _has_pending(connection, "registry")
        requests = _registry_requests(connection, rows)
        records = collector.collect_many(requests)
        terminal, retryable = _partition_registry(rows, records)
        if terminal:
            _commit_records(
                connection, output,
                [item[0] for item in terminal],
                [item[1] for item in terminal],
            )
        if retryable:
            _record_retry_errors(connection, retryable)
        observed += len(records)
        errors += len(retryable)
        processed += len(rows)
        _check_error_rate(
            observed, errors, error_rate_min_samples, error_rate_threshold
        )
    return not _has_pending(connection, "registry")


def _registry_requests(connection, rows):
    requests = []
    for row in rows:
        values = connection.execute(
            "SELECT query_kind,query_value FROM registry_value "
            "WHERE query_key=? ORDER BY query_kind,query_value",
            (row["query_key"],),
        ).fetchall()
        requests.append((row["ecosystem"], row["package_name"], values))
    return requests


def _partition_registry(rows, records):
    if len(rows) != len(records):
        raise ValueError("collector record count mismatch")
    pairs = list(zip(rows, records))
    return (
        [item for item in pairs if item[1].get("lookup_status") != "error"],
        [item for item in pairs if item[1].get("lookup_status") == "error"],
    )


def _record_retry_errors(connection, pairs) -> None:
    attempted = _now()
    with connection:
        for row, record in pairs:
            connection.execute(
                "UPDATE query SET status='retry' WHERE query_key=?",
                (row["query_key"],),
            )
            connection.execute(
                "INSERT INTO query_attempt VALUES(?,?,?,?) "
                "ON CONFLICT(query_key) DO UPDATE SET "
                "attempt_count=attempt_count+1,last_error=excluded.last_error,"
                "last_attempted_utc=excluded.last_attempted_utc",
                (row["query_key"], 1, str(record.get("error", "")), attempted),
            )


def _check_error_rate(observed, errors, minimum, threshold) -> None:
    if observed < minimum or errors / observed <= threshold:
        return
    raise CollectorQualityPause({
        "reason": "registry_error_rate_exceeded",
        "observed": observed,
        "errors": errors,
        "error_rate": errors / observed,
        "threshold": threshold,
    })


def _collect_advisory(
    connection, output, collector, batch_size, maximum
) -> bool:
    processed = 0
    while not maximum or processed < maximum:
        limit = min(batch_size, maximum - processed) if maximum else batch_size
        rows = _pending(connection, "advisory", limit)
        if not rows:
            return True
        records = collector.collect_batch(rows)
        _commit_records(connection, output, rows, records)
        processed += len(rows)
    return not _has_pending(connection, "advisory")


def _pending(connection, evidence_type: str, limit: int) -> list[dict[str, str]]:
    cursor = connection.execute(
        "SELECT query_key,ecosystem,package_name,query_kind,query_value FROM query WHERE evidence_type=? AND status='pending' ORDER BY query_key LIMIT ?",
        (evidence_type, limit),
    )
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _commit_records(connection, output: Path, rows, records) -> None:
    if len(rows) != len(records):
        raise ValueError("collector record count mismatch")
    build_evidence_bundle(records)
    payload = b"".join(
        (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        for record in records
    )
    with output.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        offset = handle.tell()
    with connection:
        connection.executemany(
            "UPDATE query SET status='done' WHERE query_key=?",
            [(row["query_key"],) for row in rows],
        )
        _set_meta(connection, "committed_offset", str(offset))


def _summary(connection, output: Path, complete: bool) -> dict[str, object]:
    counts = Counter()
    for evidence_type, status, count in connection.execute(
        "SELECT evidence_type,status,COUNT(*) FROM query GROUP BY evidence_type,status"
    ):
        counts[f"{evidence_type}_{status}"] = count
    _set_meta(connection, "complete", "true" if complete else "false")
    connection.commit()
    return {
        "generated_utc": _now(), "complete": complete,
        "evidence_output": str(output), "evidence_bytes": output.stat().st_size,
        "query_counts": dict(sorted(counts.items())),
        "legacy_reuse": True, "network_fallback": True,
    }


def _save_summary(
    connection, input_dir, output, complete, extra=None,
) -> dict[str, object]:
    summary = _summary(connection, output, complete)
    if extra:
        summary.update(extra)
    atomic_write_json(input_dir / "stage3_collector_summary.json", summary)
    return summary


def _has_pending(connection, evidence_type: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM query WHERE evidence_type=? AND status!='done' LIMIT 1",
        (evidence_type,),
    ).fetchone()
    return row is not None


def _meta(connection, key: str) -> str:
    row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""


def _set_meta(connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _legacy_directories(root: Path) -> list[Path]:
    return [root / language / "manifest_registry" for language in LANGUAGES]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> None:
    args = parse_args()
    evidence = args.evidence_output or args.input_dir / "stage3_evidence_input.jsonl"
    database = args.state_db or args.input_dir / "stage3_collector.sqlite"
    summary = run_collector(
        args.input_dir, evidence, database, args.cache_root,
        args.legacy_cache_root, _legacy_directories(args.legacy_root),
        resume=args.resume, timeout=args.timeout, retry_count=args.retry_count,
        retry_sleep=args.retry_sleep, registry_batch_size=args.registry_batch_size,
        osv_batch_size=args.osv_batch_size, maven_workers=args.maven_workers,
        registry_error_rate_threshold=args.registry_error_rate_threshold,
        registry_error_rate_min_samples=args.registry_error_rate_min_samples,
        max_registry_queries=args.max_registry_queries,
        max_advisory_queries=args.max_advisory_queries,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

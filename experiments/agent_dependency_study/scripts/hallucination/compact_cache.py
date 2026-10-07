"""Compress and deduplicate an existing RQ3 hallucination cache.

[IN]: Legacy plain files under the shared rq2_hallucinated cache.
[OUT]: Verified gzip files, content-addressed manifest blobs, and a JSON report.
[POS]: Idempotent offline cache migration; do not run beside active pipeline jobs.
[SYNC]: If migration scope or format changes, update scripts/OUTPUTS.md and the
cache section in .claude/plans/2026-07-07-hallucination-full-pipeline.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from cache_storage import (
    read_cache_bytes,
    write_content_addressed_bytes,
    write_gzip_bytes,
)
from registry_label import compact_npm_data


DATA_ROOT = Path(os.environ.get("THESIS_DATA_ROOT", r"D:\MasterThesis\thesis-work-data"))
DEFAULT_CACHE = DATA_ROOT / "cache" / "rq2_hallucinated"


@dataclass
class MigrationStats:
    started_utc: str
    scanned: int = 0
    migrated: int = 0
    original_bytes: int = 0
    compressed_logical_bytes: int = 0
    npm_compacted: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--apply", action="store_true", help="Write verified gzip files and remove matching plain files.")
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--report", type=Path, default=None)
    return parser.parse_args()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def iter_plain_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file() and not path.name.endswith((".gz", ".tmp")):
            yield path


def compact_npm_envelope(data: bytes) -> tuple[bytes, bool]:
    try:
        envelope = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return data, False
    original = envelope.get("data", {}) if isinstance(envelope, dict) else {}
    compacted = compact_npm_data(original)
    if compacted is original:
        return data, False
    envelope["data"] = compacted
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, True


def verify_and_remove(path: Path, expected: bytes) -> int:
    restored, actual_path = read_cache_bytes(path)
    if restored != expected or actual_path != Path(f"{path}.gz"):
        raise ValueError(f"gzip verification failed: {path}")
    compressed_size = actual_path.stat().st_size
    path.unlink()
    return compressed_size


def migrate_manifest(path: Path, cache_dir: Path) -> tuple[int, int, bool]:
    data = path.read_bytes()
    write_content_addressed_bytes(path, data, cache_dir / "manifest_blobs")
    return len(data), verify_and_remove(path, data), False


def migrate_regular(path: Path, compact_npm: bool) -> tuple[int, int, bool]:
    original = path.read_bytes()
    stored, compacted = compact_npm_envelope(original) if compact_npm else (original, False)
    write_gzip_bytes(path, stored)
    return len(original), verify_and_remove(path, stored), compacted


def migration_tasks(cache_dir: Path) -> Iterator[tuple[Path, str]]:
    for path in iter_plain_files(cache_dir / "manifests"):
        yield path, "manifest"
    for path in iter_plain_files(cache_dir / "maven_poms"):
        yield path, "regular"
    registry = cache_dir / "registry"
    for path in iter_plain_files(registry):
        mode = "npm" if path.parent == registry / "npm" and path.suffix == ".json" else "regular"
        yield path, mode


def write_report(path: Path, stats: MigrationStats, cache_dir: Path, complete: bool) -> None:
    payload = asdict(stats) | {"cache_dir": str(cache_dir), "complete": complete, "updated_utc": utc_now()}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def record_error(stats: MigrationStats, path: Path, error: Exception) -> None:
    stats.errors += 1
    if len(stats.error_samples) < 20:
        stats.error_samples.append(f"{path}: {type(error).__name__}: {error}")


def run_migration(args: argparse.Namespace) -> MigrationStats:
    report = args.report or args.cache_dir / "cache_migration_report.json"
    stats = MigrationStats(started_utc=utc_now())
    for path, mode in migration_tasks(args.cache_dir):
        stats.scanned += 1
        if args.apply:
            try:
                result = migrate_manifest(path, args.cache_dir) if mode == "manifest" else migrate_regular(path, mode == "npm")
                original_size, compressed_size, compacted = result
                stats.migrated += 1
                stats.original_bytes += original_size
                stats.compressed_logical_bytes += compressed_size
                stats.npm_compacted += int(compacted)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                record_error(stats, path, error)
        if args.progress_every and stats.scanned % args.progress_every == 0:
            write_report(report, stats, args.cache_dir, False)
            print(f"cache_migration scanned={stats.scanned} migrated={stats.migrated} errors={stats.errors}", flush=True)
    write_report(report, stats, args.cache_dir, args.apply and stats.errors == 0)
    return stats


def main() -> None:
    args = parse_args()
    stats = run_migration(args)
    print(json.dumps(asdict(stats), ensure_ascii=False, indent=2))
    if stats.errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

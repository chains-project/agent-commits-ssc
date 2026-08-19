"""Shared compressed cache primitives for the RQ3 hallucination pipeline.

[IN]: UTF-8 text or bytes returned by GitHub and package registries.
[OUT]: Backward-compatible plain reads and atomic deterministic `.gz` writes.
[POS]: Storage-only helper; it must not change parser or label semantics.
[SYNC]: If the on-disk format changes, update scripts/OUTPUTS.md and the cache
the cache migration notes retained in the project provenance.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import time
import uuid
from pathlib import Path


REPLACE_ATTEMPTS = 10
REPLACE_RETRY_BASE_SECONDS = 0.05
REPLACE_RETRY_MAX_SECONDS = 0.8


def gzip_path(path: Path) -> Path:
    return Path(f"{path}.gz")


def read_cache_bytes(path: Path) -> tuple[bytes | None, Path | None]:
    compressed = gzip_path(path)
    if compressed.exists():
        try:
            with gzip.open(compressed, "rb") as handle:
                return handle.read(), compressed
        except (OSError, EOFError):
            if not path.exists():
                return None, None
    if path.exists():
        return path.read_bytes(), path
    return None, None


def read_cache_text(path: Path) -> tuple[str | None, Path | None]:
    data, actual = read_cache_bytes(path)
    if data is None:
        return None, None
    return data.decode("utf-8", errors="replace"), actual


def _atomic_gzip_write(path: Path, data: bytes, compresslevel: int = 6) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".rq3-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=compresslevel, mtime=0) as handle:
                handle.write(data)
        _replace_with_retry(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_gzip_bytes(path: Path, data: bytes, compresslevel: int = 6) -> Path:
    compressed = gzip_path(path)
    _atomic_gzip_write(compressed, data, compresslevel)
    return compressed


def write_gzip_text(path: Path, text: str, compresslevel: int = 6) -> Path:
    return write_gzip_bytes(path, text.encode("utf-8"), compresslevel)


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


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".rq3-{uuid.uuid4().hex}.tmp"
    try:
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copyfile(source, temporary)
        _replace_with_retry(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_content_addressed_bytes(path: Path, data: bytes, blob_root: Path) -> Path:
    digest = hashlib.sha256(data).hexdigest()
    blob = blob_root / digest[:2] / f"{digest}.gz"
    if not blob.exists():
        _atomic_gzip_write(blob, data)
    compressed = gzip_path(path)
    _link_or_copy(blob, compressed)
    return compressed


def write_content_addressed_text(path: Path, text: str, blob_root: Path) -> Path:
    return write_content_addressed_bytes(path, text.encode("utf-8"), blob_root)

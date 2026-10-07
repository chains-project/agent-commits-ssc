"""Compact RQ2 v2 table contracts, stable IDs, and schema manifest helpers.

[IN]: Normalized or raw commit/event/episode identity parts and completed v2 CSVs.
[OUT]: Four immutable CSV contracts, deterministic IDs, strict row validation,
and an in-memory schema manifest with CSV hashes and row counts.
[POS]: Pure data-model boundary; it does not parse diffs, write outputs, query
networks, or alter the historical rq3_* Stage 1-3 production entry points.
[SYNC]: Keep tests/fixtures/rq2_synthetic_pipeline_v2/output_schema_v2.json,
RQ2_PHASE4_CORE_IMPLEMENTATION_DESIGN_2026-08-03_ZH.md, and
scripts/OUTPUTS.md synchronized with contract or ID changes.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = 2
ID_SCHEMA_VERSION = 1
ID_DIGEST_HEX_LENGTH = 32

EVENT_TYPES = ("import", "manifest_addition")
QUERY_KINDS = ("exact", "name_only", "range", "excluded", "unresolved")
REGISTRY_STATUSES = (
    "not_queried",
    "exists_before_author_date",
    "published_after_author_date",
    "no_matching_version_before_author_date",
    "current_absent_unknown",
    "excluded",
    "cannot_compare",
)
ADVISORY_STATUSES = (
    "active_before_or_at_author_date",
    "published_after_author_date",
    "withdrawn_before_author_date",
    "none_observed",
    "hydration_incomplete",
    "not_queried",
)


@dataclass(frozen=True)
class TableContract:
    filename: str
    primary_key: tuple[str, ...]
    fields: tuple[str, ...]

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "primary_key": list(self.primary_key),
            "fields": list(self.fields),
        }


_CONTRACTS = {
    "commits.csv": TableContract(
        "commits.csv",
        ("commit_id",),
        (
            "commit_id", "repo", "sha", "author_date", "agent",
            "repo_primary_language", "actual_changed_languages",
            "import_event_count", "manifest_event_count",
            "dependency_quadrant", "schema_version",
        ),
    ),
    "events.csv": TableContract(
        "events.csv",
        ("event_id",),
        (
            "event_id", "commit_id", "event_type", "path", "actual_language",
            "ecosystem", "new_line_number", "event_ordinal", "raw_target",
            "package_candidate", "version_spec", "event_status", "reason_codes",
            "schema_version",
        ),
    ),
    "episodes.csv": TableContract(
        "episodes.csv",
        ("episode_id",),
        (
            "episode_id", "commit_id", "ecosystem", "package_name",
            "dependency_quadrant", "alignment_status", "query_kind",
            "query_value", "resolution_source", "registry_status",
            "advisory_status", "reason_codes", "evidence_ids", "schema_version",
        ),
    ),
    "episode_event_links.csv": TableContract(
        "episode_event_links.csv",
        ("episode_id", "event_id"),
        ("episode_id", "event_id", "link_method", "schema_version"),
    ),
}

TABLE_CONTRACTS: Mapping[str, TableContract] = MappingProxyType(_CONTRACTS)
ENUMS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "event_type": EVENT_TYPES,
        "query_kind": QUERY_KINDS,
        "registry_status": REGISTRY_STATUSES,
        "advisory_status": ADVISORY_STATUSES,
    }
)


def _text(value: str, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not allow_empty and not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def normalize_repo(repo: str) -> str:
    normalized = _text(repo, "repo").replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) != 2:
        raise ValueError("repo must have the form owner/name")
    return "/".join(part.lower() for part in parts)


def normalize_sha(sha: str) -> str:
    normalized = _text(sha, "sha").lower()
    if re.fullmatch(r"[0-9a-f]{7,64}", normalized) is None:
        raise ValueError("sha must contain 7-64 hexadecimal characters")
    return normalized


def normalize_repo_path(path: str) -> str:
    normalized = _text(path, "path").replace("\\", "/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or ".." in parts:
        raise ValueError("path must be a non-empty repository-relative path")
    return "/".join(parts)


def _token(value: str, field: str) -> str:
    return _text(value, field).lower()


def _integer(value: int, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return value


def _stable_id(prefix: str, payload: Mapping[str, object]) -> str:
    envelope = {"id_schema_version": ID_SCHEMA_VERSION, "kind": prefix, **payload}
    canonical = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:ID_DIGEST_HEX_LENGTH]
    return f"{prefix}_{digest}"


def make_commit_id(repo: str, sha: str) -> str:
    return _stable_id(
        "cmt",
        {"repo": normalize_repo(repo), "sha": normalize_sha(sha)},
    )


def make_event_id(
    commit_id: str,
    event_type: str,
    path: str,
    new_line_number: int,
    event_ordinal: int,
    raw_target: str,
) -> str:
    normalized_type = _token(event_type, "event_type")
    _require_enum("event_type", normalized_type)
    payload = {
        "commit_id": _text(commit_id, "commit_id"),
        "event_type": normalized_type,
        "path": normalize_repo_path(path),
        "new_line_number": _integer(new_line_number, "new_line_number", 1),
        "event_ordinal": _integer(event_ordinal, "event_ordinal", 0),
        "raw_target": _text(raw_target, "raw_target"),
    }
    return _stable_id("evt", payload)


def canonical_episode_key(
    ecosystem: str,
    dependency_identity_key: str,
    query_kind: str,
    query_value: str,
) -> tuple[str, str, str, str]:
    normalized_kind = _token(query_kind, "query_kind")
    _require_enum("query_kind", normalized_kind)
    normalized_query = _text(query_value, "query_value", allow_empty=True)
    if normalized_kind in {"exact", "range"} and not normalized_query:
        raise ValueError(f"{normalized_kind} query requires query_value")
    return (
        _token(ecosystem, "ecosystem"),
        _text(dependency_identity_key, "dependency_identity_key"),
        normalized_kind,
        normalized_query,
    )


def make_episode_id(
    commit_id: str,
    ecosystem: str,
    dependency_identity_key: str,
    query_kind: str,
    query_value: str,
) -> str:
    normalized_key = canonical_episode_key(
        ecosystem, dependency_identity_key, query_kind, query_value
    )
    payload = {
        "commit_id": _text(commit_id, "commit_id"),
        "ecosystem": normalized_key[0],
        "dependency_identity_key": normalized_key[1],
        "query_kind": normalized_key[2],
        "query_value": normalized_key[3],
    }
    return _stable_id("dep", payload)


def make_evidence_id(source: str, query_key: str, payload_sha256: str) -> str:
    digest = _text(payload_sha256, "payload_sha256").lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("payload_sha256 must contain 64 hexadecimal characters")
    return _stable_id(
        "evd",
        {
            "source": _token(source, "source"),
            "query_key": _text(query_key, "query_key"),
            "payload_sha256": digest,
        },
    )


def _require_enum(field: str, value: str) -> None:
    allowed = ENUMS[field]
    if value not in allowed:
        raise ValueError(f"invalid {field}: {value!r}; expected one of {allowed}")


def table_contract(filename: str) -> TableContract:
    try:
        return TABLE_CONTRACTS[filename]
    except KeyError as exc:
        raise KeyError(f"unknown v2 table: {filename}") from exc


def schema_definition() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "id_schema_version": ID_SCHEMA_VERSION,
        "id_digest_bits": ID_DIGEST_HEX_LENGTH * 4,
        "tables": {
            name: contract.as_dict() for name, contract in TABLE_CONTRACTS.items()
        },
        "enums": {field: list(values) for field, values in ENUMS.items()},
    }


def validate_row(filename: str, row: Mapping[str, object]) -> None:
    contract = table_contract(filename)
    actual = set(row)
    expected = set(contract.fields)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"invalid fields for {filename}: missing={missing}, extra={extra}")
    _validate_primary_key(contract, row)
    _validate_schema_version(row["schema_version"])
    for field in ENUMS:
        if field in row:
            _require_enum(field, _text(row[field], field))


def _validate_primary_key(
    contract: TableContract, row: Mapping[str, object]
) -> None:
    for field in contract.primary_key:
        value = row[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"primary key field must not be empty: {field}")


def _validate_schema_version(value: object) -> None:
    if str(value) != str(SCHEMA_VERSION):
        raise ValueError(
            f"schema_version must be {SCHEMA_VERSION}, received {value!r}"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_metadata(path: Path, contract: TableContract) -> dict[str, object]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        actual_fields = tuple(reader.fieldnames or ())
        if actual_fields != contract.fields:
            raise ValueError(
                f"invalid header for {path.name}: {actual_fields!r}"
            )
        row_count = sum(1 for _row in reader)
    return {
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
        "row_count": row_count,
        "fields": list(contract.fields),
        "primary_key": list(contract.primary_key),
    }


def build_schema_manifest(
    output_dir: Path, filenames: Sequence[str] | None = None
) -> dict[str, object]:
    selected = tuple(TABLE_CONTRACTS) if filenames is None else tuple(filenames)
    tables: dict[str, object] = {}
    for filename in selected:
        contract = table_contract(filename)
        path = Path(output_dir) / filename
        if not path.is_file():
            raise FileNotFoundError(f"missing v2 table: {path}")
        tables[filename] = _csv_metadata(path, contract)
    return {
        "schema_version": SCHEMA_VERSION,
        "id_schema_version": ID_SCHEMA_VERSION,
        "table_count": len(tables),
        "tables": tables,
    }


def validate_rows(filename: str, rows: Iterable[Mapping[str, object]]) -> int:
    count = 0
    for row in rows:
        validate_row(filename, row)
        count += 1
    return count

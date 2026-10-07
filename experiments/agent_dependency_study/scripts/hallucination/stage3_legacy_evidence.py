"""Adapt legacy dependency tables into read-only evidence indexes for the collector.

[IN]: Existing registry_lookup_results.csv and advisory_lookup_results.csv across languages.

[OUT]: In-memory ecosystem/package/query indexes; preserves historical products.

[POS]: Reuses compatible evidence rather than historical final labels.

[SYNC]: Keep legacy field mappings aligned with the evidence contract and tests.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


def canonical_package(ecosystem: str, package: str) -> str:
    value = package.strip()
    if ecosystem == "PyPI":
        return value.lower().replace("_", "-").replace(".", "-")
    return value.lower() if ecosystem in {"npm", "Cargo"} else value


@dataclass(frozen=True)
class LegacyRegistryRow:
    ecosystem: str
    package_name: str
    version: str
    package_exists: bool | None
    version_exists: bool | None
    published_at: str
    source: str
    queried_at: str


@dataclass(frozen=True)
class LegacyAdvisoryRow:
    ecosystem: str
    package_name: str
    version: str
    status: str
    advisory_ids: tuple[str, ...]
    queried_at: str


class LegacyEvidenceIndex:
    def __init__(self) -> None:
        self.registry: dict[tuple[str, str], list[LegacyRegistryRow]] = defaultdict(list)
        self.advisory: dict[tuple[str, str, str], LegacyAdvisoryRow] = {}

    @classmethod
    def from_directories(cls, directories: Sequence[Path]) -> "LegacyEvidenceIndex":
        index = cls()
        for directory in directories:
            index._load_registry(directory / "registry_lookup_results.csv")
            index._load_advisory(directory / "advisory_lookup_results.csv")
        return index

    def exact_registry_record(
        self, ecosystem: str, package: str, required_versions: Iterable[str]
    ) -> dict[str, object] | None:
        rows = self.registry.get((ecosystem, canonical_package(ecosystem, package)), [])
        required = {item.strip() for item in required_versions if item.strip()}
        by_version = {row.version: row for row in rows if row.version}
        if not required or not required.issubset(by_version):
            return None
        return _registry_record(ecosystem, package, [by_version[item] for item in sorted(required)])

    def advisory_record(
        self, ecosystem: str, package: str, version: str
    ) -> dict[str, object] | None:
        key = (ecosystem, canonical_package(ecosystem, package), version.strip())
        row = self.advisory.get(key)
        if row is None or row.status != "200":
            return None
        if row.advisory_ids:
            return {"advisory_ids": list(row.advisory_ids), "queried_at": row.queried_at}
        return _empty_advisory_record(row, package)

    def _load_registry(self, path: Path) -> None:
        for row in _rows(path):
            item = _registry_row(row, path)
            key = (item.ecosystem, canonical_package(item.ecosystem, item.package_name))
            self.registry[key].append(item)

    def _load_advisory(self, path: Path) -> None:
        for row in _rows(path):
            item = _advisory_row(row, path)
            key = (item.ecosystem, canonical_package(item.ecosystem, item.package_name), item.version)
            self.advisory[key] = item


def _rows(path: Path) -> Iterable[Mapping[str, str]]:
    if not path.is_file():
        return ()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _registry_row(row: Mapping[str, str], path: Path) -> LegacyRegistryRow:
    return LegacyRegistryRow(
        row.get("ecosystem", ""), row.get("package_name", ""), row.get("version", ""),
        _boolean(row.get("package_exists_current_registry", "")),
        _boolean(row.get("version_exists_current_registry", "")),
        row.get("publish_time", ""), row.get("registry_source", "legacy_registry"),
        _mtime(path),
    )


def _advisory_row(row: Mapping[str, str], path: Path) -> LegacyAdvisoryRow:
    ids = tuple(item for item in row.get("advisory_ids", "").split(";") if item)
    return LegacyAdvisoryRow(
        row.get("ecosystem", ""), row.get("package_name", ""), row.get("version", ""),
        row.get("osv_status", ""), ids, _mtime(path),
    )


def _registry_record(
    ecosystem: str, package: str, rows: Sequence[LegacyRegistryRow]
) -> dict[str, object]:
    found = [row for row in rows if row.version_exists is True]
    package_missing = rows and all(row.package_exists is False for row in rows)
    return {
        "evidence_type": "registry", "source": "legacy_exact_registry_results",
        "ecosystem": ecosystem, "package_name": package,
        "lookup_status": "not_found" if package_missing else "ok",
        "queried_at": max(row.queried_at for row in rows),
        "versions": [
            {"version": row.version, "published_at": row.published_at}
            for row in found
        ],
        "reuse_scope": "exact_queries_only",
    }


def _empty_advisory_record(row: LegacyAdvisoryRow, package: str) -> dict[str, object]:
    return {
        "evidence_type": "advisory", "source": "OSV_legacy_zero_result",
        "ecosystem": row.ecosystem, "package_name": package,
        "query_kind": "exact", "query_value": row.version,
        "lookup_status": "ok", "queried_at": row.queried_at, "advisories": [],
        "reuse_scope": "verified_zero_result",
    }


def _boolean(value: str) -> bool | None:
    if value == "True":
        return True
    if value == "False":
        return False
    return None


def _mtime(path: Path) -> str:
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")

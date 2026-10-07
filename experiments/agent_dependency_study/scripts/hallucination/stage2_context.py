"""Compact Stage2 context index and legacy-result adapter.

[IN]: Existing import_dependency_matches.csv or fixture context rows.

[OUT]: Reusable SQLite index and lightweight commit-level context records.

[POS]: Evidence sidecar; reuses acquired records without network access.

[SYNC]: Keep stage2_episode_linker.py, stage2_build.py and context tests aligned.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import stage2_python_wheel as python_wheel


CONTEXT_SCHEMA_VERSION = 2
CONTEXT_FIELDS = (
    "repo", "sha", "source_path", "raw_target", "import_root", "ecosystem",
    "package_name", "version_spec", "resolved_version", "resolution_source",
    "dependency_group", "dependency_path", "alignment_status", "reason_codes",
)


@dataclass(frozen=True)
class ContextSource:
    language: str
    path: Path
    kind: str = "import_matches"


class NullContextLookup:
    signature = {"kind": "none", "schema_version": CONTEXT_SCHEMA_VERSION}

    def lookup(self, commit: Mapping[str, str]) -> list[dict[str, str]]:
        return []

    def close(self) -> None:
        return None


def derived_context_rows(
    commit: Mapping[str, str], events: Iterable[Mapping[str, str]]
) -> list[dict[str, str]]:
    prefixes = _repository_module_prefixes(commit.get("repo", ""))
    rows = []
    for event in events:
        target = event.get("raw_target", "")
        if event.get("ecosystem") != "Go" or not _matches_prefix(target, prefixes):
            continue
        rows.append(normalize_context({
            "repo": commit.get("repo", ""), "sha": commit.get("sha", ""),
            "source_path": event.get("path", ""), "raw_target": target,
            "import_root": target, "ecosystem": "Go", "package_name": target,
            "alignment_status": "first_party_excluded",
            "resolution_source": "repository_module_identity",
            "reason_codes": "first_party",
        }))
    return rows


def _repository_module_prefixes(repo: str) -> tuple[str, ...]:
    value = repo.strip("/")
    if not value or "/" not in value:
        return ()
    return (f"github.com/{value}", f"gitlab.com/{value}", f"bitbucket.org/{value}")


def _matches_prefix(target: str, prefixes: Sequence[str]) -> bool:
    lowered = target.lower()
    return any(lowered == prefix.lower() or lowered.startswith(prefix.lower() + "/") for prefix in prefixes)


class MemoryContextLookup:
    def __init__(self, rows: Iterable[Mapping[str, str]]) -> None:
        self.rows = [normalize_context(row) for row in rows]
        payload = json.dumps(self.rows, ensure_ascii=False, sort_keys=True)
        self.signature = {
            "kind": "memory", "schema_version": CONTEXT_SCHEMA_VERSION,
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        }

    def lookup(self, commit: Mapping[str, str]) -> list[dict[str, str]]:
        return [
            row for row in self.rows
            if row["repo"].lower() == commit["repo"].lower()
            and row["sha"] == commit["sha"].lower()
        ]

    def close(self) -> None:
        return None


class SqliteContextLookup:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"context database missing: {self.path}")
        self.connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        self.signature = _database_signature(self.path, self.connection)

    def lookup(self, commit: Mapping[str, str]) -> list[dict[str, str]]:
        fields = ",".join(CONTEXT_FIELDS)
        query = f"SELECT {fields} FROM contexts WHERE repo_key=? AND sha=?"
        params = (commit["repo"].lower(), commit["sha"].lower())
        return [dict(row) for row in self.connection.execute(query, params)]

    def close(self) -> None:
        self.connection.close()


def build_legacy_context_database(
    output: Path, sources: Sequence[ContextSource], *, rebuild: bool = False,
    batch_size: int = 10_000,
) -> dict[str, object]:
    expected = _source_manifest(sources)
    if output.is_file() and not rebuild:
        return _reuse_context_database(output, expected)
    _create_context_database(output, sources, expected, batch_size)
    connection = sqlite3.connect(f"file:{output}?mode=ro", uri=True)
    try:
        return _summary(output, connection, reused=False)
    finally:
        connection.close()


def _reuse_context_database(output: Path, expected) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{output}?mode=ro", uri=True)
    try:
        if _read_meta(connection, "source_manifest") == expected:
            return _summary(output, connection, reused=True)
    finally:
        connection.close()
    raise ValueError("context database source manifest changed; use --rebuild")


def _create_context_database(output, sources, expected, batch_size) -> None:
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(temporary)
    try:
        _create_schema(connection)
        rows = _load_sources(connection, sources, batch_size)
        _write_meta(connection, "source_manifest", expected)
        _write_meta(connection, "rows_seen", rows)
        connection.commit()
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    connection.close()
    os.replace(temporary, output)


def normalize_context(row: Mapping[str, object]) -> dict[str, str]:
    result = {field: str(row.get(field, "") or "").strip() for field in CONTEXT_FIELDS}
    result["repo"] = result["repo"].replace("\\", "/").strip("/")
    result["sha"] = result["sha"].lower()
    result["source_path"] = result["source_path"].replace("\\", "/").strip("/")
    result["dependency_path"] = result["dependency_path"].replace("\\", "/").strip("/")
    if not result["repo"] or not result["sha"] or not result["source_path"]:
        raise ValueError("context row requires repo, sha, and source_path")
    return result


def legacy_match_to_context(row: Mapping[str, str]) -> dict[str, str] | None:
    status = str(row.get("declared_dependency_match", ""))
    if not status:
        return None
    ecosystem = str(row.get("ecosystem", ""))
    source = str(row.get("resolution_source", ""))
    version_spec = str(row.get("version_spec", ""))
    workspace = _is_cargo_workspace(ecosystem, version_spec)
    if status == "observed_in_python_wheel_metadata":
        source = "python_wheel_metadata"
    reasons = _legacy_reasons(ecosystem, status, row)
    if workspace:
        reasons.update({"private_or_local", "workspace_inherited"})
    return normalize_context({
        "repo": row.get("repo", ""), "sha": row.get("sha", ""),
        "source_path": row.get("source_file", ""),
        "raw_target": row.get("import_raw", ""),
        "import_root": row.get("import_root", ""), "ecosystem": ecosystem,
        "package_name": row.get("declared_package", ""),
        "version_spec": "workspace:" if workspace else version_spec,
        "resolved_version": "" if workspace else row.get("resolved_version", ""),
        "resolution_source": source,
        "dependency_group": row.get("dependency_group", ""),
        "dependency_path": row.get("dep_file_path", ""),
        "alignment_status": (
            "private_or_local_excluded" if workspace else _legacy_alignment(status, row)
        ),
        "reason_codes": "|".join(sorted(reasons)),
    })


def legacy_dependency_to_context(row: Mapping[str, str]) -> dict[str, str] | None:
    package = str(row.get("package_name", ""))
    path = str(row.get("dep_file_path", ""))
    if not package or not path:
        return None
    source = str(row.get("resolution_source", ""))
    ecosystem = _language_ecosystem(str(row.get("repo_language", "")))
    version_spec = str(row.get("version_spec", ""))
    first_party = source == "first_party_module" or row.get("dependency_group") == "module"
    workspace = _is_cargo_workspace(ecosystem, version_spec)
    reasons = _resolution_reasons(source)
    if first_party:
        reasons.add("first_party")
    if workspace:
        reasons.update({"private_or_local", "workspace_inherited"})
    return normalize_context({
        "repo": row.get("repo", ""), "sha": row.get("sha", ""),
        "source_path": path, "raw_target": package, "import_root": package,
        "ecosystem": ecosystem,
        "package_name": package, "version_spec": "workspace:" if workspace else version_spec,
        "resolved_version": "" if workspace else row.get("resolved_version", ""),
        "resolution_source": source, "dependency_group": row.get("dependency_group", ""),
        "dependency_path": path,
        "alignment_status": (
            "first_party_excluded" if first_party
            else "private_or_local_excluded" if workspace
            else "matched_manifest"
        ),
        "reason_codes": "|".join(sorted(reasons)),
    })


def _is_cargo_workspace(ecosystem: str, version_spec: str) -> bool:
    return ecosystem == "Cargo" and bool(
        re.search(r"\bworkspace\s*=\s*true\b", version_spec, re.IGNORECASE)
    )


def _language_ecosystem(language: str) -> str:
    return {
        "TypeScript": "npm", "JavaScript": "npm", "Python": "PyPI",
        "Rust": "Cargo", "Go": "Go", "Java": "Maven",
    }.get(language, "")


def _resolution_reasons(source: str) -> set[str]:
    mapping = {
        "local_maven_property": "resolved_local_maven_property",
        "local_gradle_version_catalog": "resolved_local_gradle_catalog",
    }
    return {mapping[source]} if source in mapping else set()

def _legacy_alignment(status: str, row: Mapping[str, str]) -> str:
    if status == "first_party_module_import":
        return "first_party_excluded"
    if status in {
        "mapping_unknown", "not_observed_in_parsed_dependency_files",
        "cannot_compare_no_parsed_dependencies",
    }:
        return "mapping_uncertain"
    if "manifest_and_lockfile" in status:
        return "matched_manifest_and_lock"
    if "lockfile_possible_transitive" in status:
        return "lockfile_only_observed"
    changed = str(row.get("dependency_file_changed_in_diff", "")).lower() == "true"
    return "matched_manifest" if changed else "matched_existing_manifest"


def _legacy_reasons(ecosystem: str, status: str, row: Mapping[str, str]) -> set[str]:
    if status == "first_party_module_import":
        return {"first_party"}
    if status in {
        "mapping_unknown", "not_observed_in_parsed_dependency_files",
        "cannot_compare_no_parsed_dependencies",
    }:
        reasons = {"mapping_uncertain"}
        if ecosystem == "PyPI":
            reasons.add("python_alias_not_found")
        return reasons
    if status == "observed_in_python_wheel_metadata":
        return {"python_wheel_metadata"} | _resolution_reasons(
            str(row.get("resolution_source", ""))
        )
    if "lockfile_possible_transitive" in status:
        return {"possible_transitive_dependency"}
    return _resolution_reasons(str(row.get("resolution_source", "")))


def _create_schema(connection: sqlite3.Connection) -> None:
    columns = ",".join(f"{field} TEXT NOT NULL" for field in CONTEXT_FIELDS)
    unique = (
        "repo_key,sha,source_path,raw_target,package_name,resolved_version,"
        "alignment_status,resolution_source"
    )
    connection.execute(
        f"CREATE TABLE contexts (repo_key TEXT NOT NULL,{columns},UNIQUE({unique}))"
    )
    connection.execute("CREATE INDEX contexts_commit ON contexts(repo_key,sha)")
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    python_wheel.create_schema(connection)


def _load_sources(
    connection: sqlite3.Connection, sources: Sequence[ContextSource], batch_size: int
) -> int:
    rows_seen, batch = 0, []
    for source in sources:
        with source.path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle)
            if source.kind in python_wheel.SOURCE_KINDS:
                rows_seen += python_wheel.load_source(
                    connection, source.kind, reader, batch_size
                )
                continue
            for row in reader:
                rows_seen += 1
                converter = legacy_dependency_to_context if source.kind == "parsed_dependencies" else legacy_match_to_context
                context = converter(row)
                if context:
                    batch.append(_insert_values(context))
                if len(batch) >= batch_size:
                    _insert_batch(connection, batch)
                    batch = []
    for context in python_wheel.iter_bridge_contexts(connection):
        batch.append(_insert_values(normalize_context(context)))
        if len(batch) >= batch_size:
            _insert_batch(connection, batch)
            batch = []
    _insert_batch(connection, batch)
    return rows_seen


def _insert_values(row: Mapping[str, str]) -> tuple[str, ...]:
    return (row["repo"].lower(), *(row[field] for field in CONTEXT_FIELDS))


def _insert_batch(connection: sqlite3.Connection, batch: Sequence[tuple[str, ...]]) -> None:
    if not batch:
        return
    marks = ",".join("?" for _ in range(len(CONTEXT_FIELDS) + 1))
    connection.executemany(f"INSERT OR IGNORE INTO contexts VALUES ({marks})", batch)
    connection.commit()


def _source_manifest(sources: Sequence[ContextSource]) -> list[dict[str, object]]:
    manifest = []
    for source in sources:
        record = {
            "language": source.language, "kind": source.kind,
            "path": str(source.path.resolve()),
            "bytes": source.path.stat().st_size,
            "mtime_ns": source.path.stat().st_mtime_ns,
        }
        if source.kind in python_wheel.SOURCE_KINDS:
            record["sha256"] = _file_sha256(source.path)
        manifest.append(record)
    return manifest


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_meta(connection: sqlite3.Connection, key: str, value: object) -> None:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    connection.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, text))


def _read_meta(connection: sqlite3.Connection, key: str) -> object:
    row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _database_signature(path: Path, connection: sqlite3.Connection) -> dict[str, object]:
    return {
        "kind": "sqlite", "schema_version": CONTEXT_SCHEMA_VERSION,
        "path": str(path.resolve()), "bytes": path.stat().st_size,
        "source_manifest": _read_meta(connection, "source_manifest"),
    }


def _summary(
    path: Path, connection: sqlite3.Connection, *, reused: bool
) -> dict[str, object]:
    count = int(connection.execute("SELECT COUNT(*) FROM contexts").fetchone()[0])
    summary = {
        "schema_version": CONTEXT_SCHEMA_VERSION, "database": str(path.resolve()),
        "context_rows": count, "rows_seen": _read_meta(connection, "rows_seen"),
        "reused": reused, "source_manifest": _read_meta(connection, "source_manifest"),
    }
    wheel_statuses = python_wheel.statuses(connection)
    if wheel_statuses:
        summary["python_wheel_statuses"] = wheel_statuses
    return summary

"""Python wheel evidence storage and conservative commit-scoped bridges.

[IN]: Parsed Python dependencies and the global wheel import-root cache.
[OUT]: Auxiliary SQLite evidence plus normalized Stage 2 context bridge rows.
[POS]: Offline Stage 2 helper; it never fetches wheels or mutates canonical tables.
[SYNC]: Keep rq2_stage2_context.py, Phase 9 runner, tests, and OUTPUTS.md aligned.
"""

from __future__ import annotations

import re
import sqlite3
from itertools import groupby
from typing import Iterable, Iterator, Mapping


SOURCE_KINDS = {"python_dependencies", "python_wheel_map"}
BRIDGE_COLUMNS = (
    "repo_key", "repo", "sha", "import_root", "package_key",
    "package_name", "version_spec", "resolved_version", "exact_version",
    "resolution_source", "dependency_group", "dependency_path", "changed",
)


def create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE python_dependencies (
        repo_key TEXT NOT NULL,repo TEXT NOT NULL,sha TEXT NOT NULL,
        package_key TEXT NOT NULL,package_name TEXT NOT NULL,
        version_spec TEXT NOT NULL,resolved_version TEXT NOT NULL,
        exact_version TEXT NOT NULL,resolution_source TEXT NOT NULL,
        dependency_group TEXT NOT NULL,dependency_path TEXT NOT NULL,
        changed TEXT NOT NULL,
        UNIQUE(repo_key,sha,package_key,exact_version,dependency_path,resolution_source)
    )""")
    connection.execute("""CREATE TABLE python_wheel_evidence (
        package_key TEXT NOT NULL,package_name TEXT NOT NULL,version TEXT NOT NULL,
        import_root TEXT NOT NULL,status TEXT NOT NULL,source TEXT NOT NULL,
        wheel_filename TEXT NOT NULL,wheel_size TEXT NOT NULL,error TEXT NOT NULL
    )""")
    connection.execute(
        "CREATE INDEX python_deps_join "
        "ON python_dependencies(package_key,exact_version)"
    )
    connection.execute(
        "CREATE INDEX python_wheel_join "
        "ON python_wheel_evidence(package_key,version,status)"
    )


def load_source(
    connection: sqlite3.Connection, kind: str, rows: Iterable[Mapping[str, str]],
    batch_size: int,
) -> int:
    if kind == "python_dependencies":
        return _load_dependencies(connection, rows, batch_size)
    if kind == "python_wheel_map":
        return _load_wheels(connection, rows, batch_size)
    raise ValueError(f"unsupported Python wheel source kind: {kind}")


def _load_dependencies(connection, rows, batch_size) -> int:
    count, batch = 0, []
    for row in rows:
        count += 1
        values = _dependency_values(row)
        if values:
            batch.append(values)
        if len(batch) >= batch_size:
            _insert_many(connection, "python_dependencies", 12, batch)
            batch = []
    _insert_many(connection, "python_dependencies", 12, batch)
    return count


def _dependency_values(row: Mapping[str, str]) -> tuple[str, ...] | None:
    if row.get("repo_language") != "Python":
        return None
    version = _exact_version(row)
    package = str(row.get("package_name", "")).strip()
    repo = str(row.get("repo", "")).replace(chr(92), "/").strip("/")
    sha = str(row.get("sha", "")).lower()
    path = str(row.get("dep_file_path", "")).replace(chr(92), "/").strip("/")
    if not version or not package or not repo or not sha or not path:
        return None
    return (
        repo.lower(), repo, sha, _package_key(package), package,
        str(row.get("version_spec", "")).strip(),
        str(row.get("resolved_version", "")).strip(), version,
        str(row.get("resolution_source", "")).strip(),
        str(row.get("dependency_group", "")).strip(), path,
        str(row.get("dependency_file_changed_in_diff", "")).lower(),
    )


def _exact_version(row: Mapping[str, str]) -> str:
    resolved = str(row.get("resolved_version", "")).strip()
    if resolved:
        return resolved
    if row.get("version_kind") != "exact":
        return ""
    value = str(row.get("version_spec", "")).split(";", 1)[0].strip()
    for prefix in ("===", "=="):
        if value.startswith(prefix):
            return value[len(prefix):].strip()
    return value


def _load_wheels(connection, rows, batch_size) -> int:
    count, batch = 0, []
    for row in rows:
        count += 1
        package = str(row.get("package_name", "")).strip()
        version = str(row.get("version", "")).strip()
        if package and version:
            batch.append(_wheel_values(row, package, version))
        if len(batch) >= batch_size:
            _insert_many(connection, "python_wheel_evidence", 9, batch)
            batch = []
    _insert_many(connection, "python_wheel_evidence", 9, batch)
    return count


def _wheel_values(row, package, version) -> tuple[str, ...]:
    return (
        _package_key(package), package, version,
        str(row.get("import_root", "")).strip(),
        str(row.get("status", "")).strip(), str(row.get("source", "")).strip(),
        str(row.get("wheel_filename", "")).strip(),
        str(row.get("wheel_size", "")).strip(),
        str(row.get("error", "")).strip(),
    )


def iter_bridge_contexts(
    connection: sqlite3.Connection,
) -> Iterator[dict[str, str]]:
    rows = _matched_rows(connection)
    key = lambda row: (row["repo_key"], row["sha"], row["import_root"])
    for (_repo, _sha, root), values in groupby(rows, key=key):
        grouped = list(values)
        identities = {
            (row["package_key"], row["exact_version"]) for row in grouped
        }
        if len(identities) == 1:
            yield _positive_bridge(grouped[0], root)
        else:
            yield _ambiguous_bridge(grouped[0], root)


def _matched_rows(connection: sqlite3.Connection) -> Iterator[dict[str, str]]:
    query = """SELECT d.repo_key,d.repo,d.sha,w.import_root,d.package_key,
        d.package_name,d.version_spec,d.resolved_version,d.exact_version,
        d.resolution_source,d.dependency_group,d.dependency_path,d.changed
        FROM python_dependencies d JOIN python_wheel_evidence w
        ON d.package_key=w.package_key AND d.exact_version=w.version
        WHERE w.status='ok' AND w.import_root<>''
        ORDER BY d.repo_key,d.sha,w.import_root,d.package_key,d.exact_version,
        CASE WHEN d.resolved_version<>'' THEN 0 ELSE 1 END,d.dependency_path"""
    for values in connection.execute(query):
        yield dict(zip(BRIDGE_COLUMNS, values))


def _positive_bridge(
    row: Mapping[str, str], root: str,
) -> dict[str, str]:
    reasons = {"python_wheel_metadata"}
    if "lockfile_possible_transitive" in row["resolution_source"]:
        reasons.add("possible_transitive_dependency")
    return {
        "repo": row["repo"], "sha": row["sha"], "source_path": "*",
        "raw_target": root, "import_root": root, "ecosystem": "PyPI",
        "package_name": row["package_name"],
        "version_spec": row["version_spec"],
        "resolved_version": row["exact_version"],
        "resolution_source": "python_wheel_metadata",
        "dependency_group": row["dependency_group"],
        "dependency_path": row["dependency_path"],
        "alignment_status": _alignment(row),
        "reason_codes": "|".join(sorted(reasons)),
    }


def _ambiguous_bridge(
    row: Mapping[str, str], root: str,
) -> dict[str, str]:
    return {
        "repo": row["repo"], "sha": row["sha"], "source_path": "*",
        "raw_target": root, "import_root": root, "ecosystem": "PyPI",
        "package_name": "", "version_spec": "", "resolved_version": "",
        "resolution_source": "python_wheel_metadata", "dependency_group": "",
        "dependency_path": "", "alignment_status": "mapping_uncertain",
        "reason_codes": (
            "mapping_uncertain|python_wheel_multiple_candidates"
        ),
    }


def _alignment(row: Mapping[str, str]) -> str:
    source = row["resolution_source"]
    if "manifest+lockfile" in source:
        return "matched_manifest_and_lock"
    if "lockfile_possible_transitive" in source:
        return "lockfile_only_observed"
    if row["changed"] == "true":
        return "matched_manifest"
    return "matched_existing_manifest"


def statuses(connection: sqlite3.Connection) -> dict[str, int]:
    query = (
        "SELECT status,COUNT(*) FROM python_wheel_evidence "
        "GROUP BY status ORDER BY status"
    )
    try:
        rows = connection.execute(query)
    except sqlite3.OperationalError:
        return {}
    return {str(status): int(count) for status, count in rows}


def _insert_many(connection, table: str, columns: int, rows) -> None:
    if not rows:
        return
    marks = ",".join("?" for _ in range(columns))
    connection.executemany(
        f"INSERT OR IGNORE INTO {table} VALUES ({marks})", rows
    )
    connection.commit()


def _package_key(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip().lower())

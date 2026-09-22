"""Deterministic read-only scanner for frozen harness/model attribution evidence."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


FIELD_BITS = {
    "full_commit_message": 1,
    "trailer_line": 2,
    "message_first_line": 4,
    "author_email": 8,
    "committer_email": 16,
    "author_login": 32,
    "committer_login": 64,
}
SCANNED_BIT = 128
RAW_RECORD_BIT = 256
VALID_SUPPLEMENT_AGENTS = {"codex", "copilot", "cursor"}
VALID_TIERS = {"main", "audit", "monthly_audit"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def source_rows(manifest: dict[str, Any], role: str) -> list[dict[str, Any]]:
    return sorted(
        [row for row in manifest["sources"] if row["logical_role"] == role],
        key=lambda row: row["resolved_path"],
    )


def verify_frozen_sources(manifest: dict[str, Any]) -> None:
    for row in manifest["sources"]:
        if row["used_by_scanner"] != "true":
            continue
        path = Path(row["resolved_path"])
        if not path.is_file() or sha256_file(path) != row["sha256"]:
            raise RuntimeError(f"Frozen input mismatch: {path}")


def load_population(path: Path, branch: str) -> tuple[dict[str, int], int]:
    states: dict[str, int] = {}
    invalid_physical_rows = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        def non_nul_lines() -> Iterable[str]:
            nonlocal invalid_physical_rows
            for line in handle:
                if "\0" in line:
                    invalid_physical_rows += 1
                else:
                    yield line

        for row in csv.DictReader(non_nul_lines()):
            key = row.get("repo_sha", "")
            if key and valid_population_row(row, branch):
                states.setdefault(key, 0)
    return states, invalid_physical_rows


def valid_population_row(row: dict[str, str], branch: str) -> bool:
    if branch != "supplementary_4h":
        return True
    return row.get("agent") in VALID_SUPPLEMENT_AGENTS and row.get("tier") in VALID_TIERS


def compile_registry(registry: dict[str, Any]) -> list[tuple[dict[str, Any], re.Pattern[str] | None]]:
    compiled = []
    for pattern in registry["patterns"]:
        regex = re.compile(pattern["pattern"]) if pattern["pattern_type"] == "regex" else None
        compiled.append((pattern, regex))
    return compiled


def extract_fields(envelope: dict[str, Any]) -> tuple[dict[str, str], list[str], str]:
    item = envelope.get("item") or {}
    commit = item.get("commit") or {}
    message = str(commit.get("message") or "")
    author = commit.get("author") or {}
    committer = commit.get("committer") or {}
    fields = {
        "full_commit_message": message,
        "message_first_line": message.splitlines()[0] if message else "",
        "author_email": str(author.get("email") or ""),
        "committer_email": str(committer.get("email") or ""),
        "author_login": str((item.get("author") or {}).get("login") or ""),
        "committer_login": str((item.get("committer") or {}).get("login") or ""),
    }
    trailers = [line for line in message.splitlines() if re.match(r"^[A-Za-z][A-Za-z-]*:", line)]
    month = str(author.get("date") or "")[:7] or "UNKNOWN"
    return fields, trailers, month


def availability_bits(fields: dict[str, str], raw_available: bool = True) -> int:
    bits = 0
    if raw_available:
        bits |= RAW_RECORD_BIT
        bits |= FIELD_BITS["full_commit_message"] | FIELD_BITS["trailer_line"] | FIELD_BITS["message_first_line"]
    for field, value in fields.items():
        if value and field not in {"full_commit_message", "message_first_line"}:
            bits |= FIELD_BITS[field]
    return bits


def excluded(signal_id: str, text: str, start: int, end: int) -> bool:
    context = text[max(0, start - 8) : min(len(text), end + 8)].lower()
    if signal_id == "m0_qwen_model_name" and re.search(r"qwen\s+code", context):
        return True
    if signal_id == "m0_kimi_model_name" and re.search(r"kimi\s+code", context):
        return True
    return False


def match_values(pattern: dict[str, Any], regex: re.Pattern[str] | None, text: str) -> Iterable[tuple[int, int, str]]:
    if pattern["pattern_type"] == "exact_ci":
        if text.casefold() == pattern["pattern"].casefold():
            yield 0, len(text), text
        return
    assert regex is not None
    for match in regex.finditer(text):
        if not excluded(pattern["signal_id"], text, match.start(), match.end()):
            yield match.start(), match.end(), match.group(0)


def redact_span(value: str, field: str) -> str:
    if field.endswith("email"):
        return "<matched-email>"
    value = re.sub(r"https?://\S+", "<url>", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+", "<email>", value)
    return value.replace("\r", " ").replace("\n", " ")[:160]


def context_category(field: str) -> str:
    return {
        "full_commit_message": "commit_message_text",
        "trailer_line": "structured_trailer_line",
        "message_first_line": "commit_subject",
        "author_email": "commit_author_email",
        "committer_email": "commit_committer_email",
        "author_login": "github_author_login",
        "committer_login": "github_committer_login",
    }[field]


def create_database(path: Path, resume: bool) -> sqlite3.Connection:
    if path.exists() and not resume:
        raise FileExistsError(f"Refusing to overwrite {path}")
    connection = sqlite3.connect(path)
    if not path.exists() or not resume:
        connection.executescript(DB_SCHEMA)
    return connection


DB_SCHEMA = """
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;
CREATE TABLE availability(branch TEXT, repo_key TEXT, bitmask INTEGER, PRIMARY KEY(branch, repo_key));
CREATE TABLE provenance(branch TEXT, repo_key TEXT, agent TEXT, tier TEXT, channel TEXT, mode TEXT,
 source_artifact TEXT, source_row_key TEXT, PRIMARY KEY(branch, repo_key, agent, tier, channel, mode, source_artifact));
CREATE TABLE hits(branch TEXT, repo_key TEXT, signal_id TEXT, field TEXT, evidence_tier TEXT,
 entity_type TEXT, canonical_name TEXT, assertion_type TEXT, signal_span TEXT, context_category TEXT,
 author_month TEXT, source_artifact TEXT,
 PRIMARY KEY(branch, repo_key, signal_id, field, signal_span, context_category));
CREATE INDEX hits_branch_repo ON hits(branch, repo_key);
CREATE INDEX hits_branch_tier ON hits(branch, evidence_tier);
"""


def scan_fields(compiled: list[tuple[dict[str, Any], re.Pattern[str] | None]], fields: dict[str, str], trailers: list[str]) -> list[tuple[Any, ...]]:
    hits: list[tuple[Any, ...]] = []
    for pattern, regex in compiled:
        for field in pattern["applicable_fields"]:
            values = trailers if field == "trailer_line" else [fields.get(field, "")]
            for text in values:
                for _, _, span in match_values(pattern, regex, text):
                    hits.append((pattern, field, redact_span(span, field), context_category(field)))
    return hits


def provenance_tuple(branch: str, envelope: dict[str, Any], artifact: str, line_number: int) -> tuple[str, ...]:
    repo_sha = str(envelope.get("repo_sha") or "")
    source_key = stable_key(f"{branch}|{artifact}|{line_number}|{repo_sha}")
    return (branch, stable_key(repo_sha), str(envelope.get("agent") or ""), str(envelope.get("tier") or ""), str(envelope.get("channel") or ""), str(envelope.get("mode") or ""), artifact, source_key)


def hit_tuple(branch: str, repo_sha: str, hit: tuple[Any, ...], month: str, artifact: str) -> tuple[str, ...]:
    pattern, field, span, category = hit
    return (branch, stable_key(repo_sha), pattern["signal_id"], field, pattern["evidence_tier"], pattern["entity_type"], pattern["canonical_name"], pattern["assertion_type"], span, category, month, artifact)


def flush_batches(connection: sqlite3.Connection, provenance: list[tuple[Any, ...]], hits: list[tuple[Any, ...]]) -> None:
    connection.executemany("INSERT OR IGNORE INTO provenance VALUES (?,?,?,?,?,?,?,?)", provenance)
    connection.executemany("INSERT OR IGNORE INTO hits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", hits)
    provenance.clear()
    hits.clear()


def scan_branch(branch: str, states: dict[str, int], raw_paths: list[Path], compiled: list[Any], connection: sqlite3.Connection) -> dict[str, Any]:
    provenance_batch: list[tuple[Any, ...]] = []
    hit_batch: list[tuple[Any, ...]] = []
    raw_records = 0
    for path in raw_paths:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                raw_records += 1
                envelope = json.loads(line)
                repo_sha = str(envelope.get("repo_sha") or "")
                if repo_sha not in states:
                    continue
                provenance_batch.append(provenance_tuple(branch, envelope, path.name, line_number))
                fields, trailers, month = extract_fields(envelope)
                states[repo_sha] |= availability_bits(fields)
                if not states[repo_sha] & SCANNED_BIT:
                    for hit in scan_fields(compiled, fields, trailers):
                        hit_batch.append(hit_tuple(branch, repo_sha, hit, month, path.name))
                    states[repo_sha] |= SCANNED_BIT
                if len(provenance_batch) >= 10000:
                    flush_batches(connection, provenance_batch, hit_batch)
        flush_batches(connection, provenance_batch, hit_batch)
        connection.commit()
    write_availability(connection, branch, states)
    return branch_availability_summary(states, raw_records)


def write_availability(connection: sqlite3.Connection, branch: str, states: dict[str, int]) -> None:
    rows = ((branch, stable_key(key), bits & ~SCANNED_BIT) for key, bits in states.items())
    connection.executemany("INSERT OR REPLACE INTO availability VALUES (?,?,?)", rows)
    connection.commit()


def branch_availability_summary(states: dict[str, int], raw_records: int) -> dict[str, Any]:
    summary: dict[str, Any] = {"population_unique_repo_sha": len(states), "raw_records_read": raw_records}
    for field, bit in FIELD_BITS.items():
        summary[f"{field}_available"] = sum(bool(value & bit) for value in states.values())
    summary["raw_mapping_unavailable"] = sum(not bool(value & RAW_RECORD_BIT) for value in states.values())
    return summary


def union_availability(connection: sqlite3.Connection) -> dict[str, Any]:
    query = "SELECT repo_key, bitmask FROM availability ORDER BY repo_key, branch"
    counts = Counter()
    population = 0
    current_key = ""
    current_bits = 0
    for repo_key, bits in connection.execute(query):
        if current_key and repo_key != current_key:
            population += 1
            for field, bit in FIELD_BITS.items():
                counts[field] += bool(current_bits & bit)
            current_bits = 0
        current_key = repo_key
        current_bits |= bits
    if current_key:
        population += 1
        for field, bit in FIELD_BITS.items():
            counts[field] += bool(current_bits & bit)
    result: dict[str, Any] = {"population_unique_repo_sha": population}
    for field in FIELD_BITS:
        result[f"{field}_available"] = counts[field]
    raw_available = connection.execute("SELECT COUNT(DISTINCT repo_key) FROM provenance").fetchone()[0]
    result["raw_mapping_unavailable"] = population - raw_available
    return result


def tier_counts(connection: sqlite3.Connection, branch_sql: str, params: tuple[Any, ...]) -> dict[str, int]:
    query = f"SELECT evidence_tier, COUNT(DISTINCT repo_key) FROM hits WHERE {branch_sql} GROUP BY evidence_tier"
    counts = {tier: 0 for tier in ["H0", "H1", "M0", "M1", "M2"]}
    counts.update(dict(connection.execute(query, params).fetchall()))
    return counts


def combination_counts(connection: sqlite3.Connection, branch_sql: str, params: tuple[Any, ...]) -> dict[str, int]:
    query = f"SELECT repo_key, GROUP_CONCAT(DISTINCT evidence_tier) FROM hits WHERE {branch_sql} GROUP BY repo_key"
    hm0 = 0
    for _, tiers_text in connection.execute(query, params):
        tiers = set((tiers_text or "").split(","))
        hm0 += bool("H1" in tiers and ({"M0", "M1"} & tiers))
    return {"HM0": hm0, "HM1": 0, "HM2": 0}


def field_status_rows(connection: sqlite3.Connection, branch: str, population: int) -> list[dict[str, Any]]:
    rows = []
    for field, bit in FIELD_BITS.items():
        available = connection.execute("SELECT COUNT(*) FROM availability WHERE branch=? AND bitmask & ? != 0", (branch, bit)).fetchone()[0]
        matched = connection.execute("SELECT COUNT(DISTINCT repo_key) FROM hits WHERE branch=? AND field=?", (branch, field)).fetchone()[0]
        rows.append({"branch": branch, "field": field, "field_available_match": matched, "field_available_no_match": available - matched, "field_unavailable": population - available})
    return rows


def signal_coverage_rows(connection: sqlite3.Connection, registry: dict[str, Any], branches: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for branch, summary in branches.items():
        for pattern in registry["patterns"]:
            for field in pattern["applicable_fields"]:
                available = summary[f"{field}_available"]
                if branch == "merged_expanded":
                    sql = "SELECT COUNT(DISTINCT repo_key) FROM hits WHERE branch IN ('matched_10min','supplementary_4h') AND signal_id=? AND field=?"
                    matched = connection.execute(sql, (pattern["signal_id"], field)).fetchone()[0]
                else:
                    sql = "SELECT COUNT(DISTINCT repo_key) FROM hits WHERE branch=? AND signal_id=? AND field=?"
                    matched = connection.execute(sql, (branch, pattern["signal_id"], field)).fetchone()[0]
                rows.append({"branch": branch, "signal_id": pattern["signal_id"], "field": field, "evidence_tier": pattern["evidence_tier"], "available_denominator": available, "matched_repo_sha": matched, "observable_match_coverage": matched / available if available else None})
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def deterministic_gzip_text(path: Path) -> io.TextIOWrapper:
    raw = path.open("wb")
    zipped = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
    return io.TextIOWrapper(zipped, encoding="utf-8", newline="")


def write_hit_ledger(connection: sqlite3.Connection, path: Path) -> int:
    query = """SELECT h.branch,h.repo_key,h.signal_id,h.field,h.evidence_tier,h.entity_type,
 h.canonical_name,h.assertion_type,h.signal_span,h.context_category,h.author_month,
 (SELECT GROUP_CONCAT(v,';') FROM (SELECT DISTINCT source_artifact AS v FROM provenance p WHERE p.branch=h.branch AND p.repo_key=h.repo_key ORDER BY v)),
 (SELECT GROUP_CONCAT(v,';') FROM (SELECT DISTINCT source_row_key AS v FROM provenance p WHERE p.branch=h.branch AND p.repo_key=h.repo_key ORDER BY v)),
 (SELECT GROUP_CONCAT(v,';') FROM (SELECT DISTINCT agent AS v FROM provenance p WHERE p.branch=h.branch AND p.repo_key=h.repo_key ORDER BY v)),
 (SELECT GROUP_CONCAT(v,';') FROM (SELECT DISTINCT channel AS v FROM provenance p WHERE p.branch=h.branch AND p.repo_key=h.repo_key ORDER BY v)), ''
 FROM hits h ORDER BY h.branch,h.repo_key,h.signal_id,h.field,h.signal_span"""
    header = ["branch", "repo_sha_key", "signal_id", "field", "evidence_tier", "entity_type", "canonical_name", "assertion_type", "signal_span", "context_category", "author_month", "source_artifacts", "source_row_keys", "attribution_agents", "attribution_channels", "binding_relation"]
    count = 0
    with deterministic_gzip_text(path) as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for row in connection.execute(query):
            writer.writerow(row)
            count += 1
    return count


def distribution_rows(connection: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    if kind == "multi_label":
        query = "SELECT branch,n,COUNT(*) FROM (SELECT branch,repo_key,COUNT(DISTINCT signal_id) n FROM hits GROUP BY branch,repo_key) GROUP BY branch,n ORDER BY branch,n"
        rows = [{"branch": b, "distinct_signal_count": n, "repo_sha_count": c} for b, n, c in connection.execute(query)]
        merged = "SELECT n,COUNT(*) FROM (SELECT repo_key,COUNT(DISTINCT signal_id) n FROM hits GROUP BY repo_key) GROUP BY n ORDER BY n"
        rows.extend({"branch": "merged_expanded", "distinct_signal_count": n, "repo_sha_count": c} for n, c in connection.execute(merged))
        return rows
    query = "SELECT branch,author_month,evidence_tier,COUNT(DISTINCT repo_key) FROM hits GROUP BY branch,author_month,evidence_tier ORDER BY branch,author_month,evidence_tier"
    rows = [{"branch": b, "author_month": m, "evidence_tier": t, "repo_sha_count": c} for b, m, t, c in connection.execute(query)]
    merged = "SELECT author_month,evidence_tier,COUNT(DISTINCT repo_key) FROM hits GROUP BY author_month,evidence_tier ORDER BY author_month,evidence_tier"
    rows.extend({"branch": "merged_expanded", "author_month": m, "evidence_tier": t, "repo_sha_count": c} for m, t, c in connection.execute(merged))
    return rows


def merged_field_status_rows(connection: sqlite3.Connection, summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    population = summary["population_unique_repo_sha"]
    for field in FIELD_BITS:
        available = summary[f"{field}_available"]
        sql = "SELECT COUNT(DISTINCT repo_key) FROM hits WHERE branch IN ('matched_10min','supplementary_4h') AND field=?"
        matched = connection.execute(sql, (field,)).fetchone()[0]
        rows.append({"branch": "merged_expanded", "field": field, "field_available_match": matched, "field_available_no_match": available - matched, "field_unavailable": population - available})
    return rows


def merged_signal_counts(connection: sqlite3.Connection) -> tuple[dict[str, int], dict[str, int]]:
    tiers = tier_counts(connection, "branch IN ('matched_10min','supplementary_4h')", ())
    combos = combination_counts(connection, "branch IN ('matched_10min','supplementary_4h')", ())
    return tiers, combos


def output_hashes(output: Path, names: list[str]) -> dict[str, str]:
    return {name: sha256_file(output / name) for name in names}


def configured_branch(branch: str, population_path: Path, raw_paths: list[Path], compiled: list[Any], connection: sqlite3.Connection) -> dict[str, Any]:
    states, invalid = load_population(population_path, branch)
    summary = scan_branch(branch, states, raw_paths, compiled, connection)
    summary["invalid_physical_rows_skipped"] = invalid
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume-existing-work", action="store_true")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    manifest = load_json(args.input_manifest.resolve())
    registry = load_json(args.registry.resolve())
    verify_frozen_sources(manifest)
    connection = create_database(output / "OFFLINE_SCAN_WORK.sqlite", args.resume_existing_work)
    compiled = compile_registry(registry)
    matched_path = Path(source_rows(manifest, "matched_population")[0]["resolved_path"])
    supplement_path = Path(source_rows(manifest, "supplement_channel_rows")[0]["resolved_path"])
    matched_raw = [Path(row["resolved_path"]) for role in ("matched_raw_message", "matched_retry_raw_message") for row in source_rows(manifest, role)]
    supplement_raw = [Path(row["resolved_path"]) for row in source_rows(manifest, "supplement_raw_message")]
    branches = {
        "matched_10min": configured_branch("matched_10min", matched_path, matched_raw, compiled, connection),
        "supplementary_4h": configured_branch("supplementary_4h", supplement_path, supplement_raw, compiled, connection),
    }
    branches["merged_expanded"] = union_availability(connection)
    tier_summary = {}
    for branch in ("matched_10min", "supplementary_4h"):
        tier_summary[branch] = tier_counts(connection, "branch=?", (branch,)) | combination_counts(connection, "branch=?", (branch,))
    merged_tiers, merged_combos = merged_signal_counts(connection)
    tier_summary["merged_expanded"] = merged_tiers | merged_combos
    field_rows = []
    for branch in ("matched_10min", "supplementary_4h"):
        field_rows.extend(field_status_rows(connection, branch, branches[branch]["population_unique_repo_sha"]))
    field_rows.extend(merged_field_status_rows(connection, branches["merged_expanded"]))
    coverage_rows = signal_coverage_rows(connection, registry, branches)
    write_csv(output / "OFFLINE_FIELD_STATUS.csv", field_rows)
    write_csv(output / "OFFLINE_SIGNAL_COVERAGE_SUMMARY.csv", coverage_rows)
    write_csv(output / "OFFLINE_MULTI_LABEL_DISTRIBUTION.csv", distribution_rows(connection, "multi_label"))
    write_csv(output / "OFFLINE_SIGNAL_TIME_DISTRIBUTION.csv", distribution_rows(connection, "time"))
    ledger_rows = write_hit_ledger(connection, output / "OFFLINE_SIGNAL_HITS.csv.gz")
    connection.commit()
    connection.close()
    summary = {"schema_version": "offline-signal-summary-v1", "branches": branches, "evidence_tier_counts": tier_summary, "hit_ledger_rows": ledger_rows, "interpretation": "counts are unique repository-SHA observable matches in a four-product-conditioned corpus; they are not prevalence or recall"}
    (output / "OFFLINE_SIGNAL_COVERAGE_SUMMARY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    outputs = ["OFFLINE_FIELD_STATUS.csv", "OFFLINE_SIGNAL_COVERAGE_SUMMARY.csv", "OFFLINE_MULTI_LABEL_DISTRIBUTION.csv", "OFFLINE_SIGNAL_TIME_DISTRIBUTION.csv", "OFFLINE_SIGNAL_HITS.csv.gz", "OFFLINE_SIGNAL_COVERAGE_SUMMARY.json", "OFFLINE_SCAN_WORK.sqlite"]
    run_manifest = {
        "schema_version": "offline-scan-run-v1",
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scanner_sha256": sha256_file(Path(__file__).resolve()),
        "input_manifest_sha256": sha256_file(args.input_manifest.resolve()),
        "registry_version": registry["registry_version"],
        "registry_sha256": sha256_file(args.registry.resolve()),
        "parameters": {"repository_sha_dedup": True, "raw_jsonl_streaming": True, "patch_scan": "not_run_no_registry_patch_pattern", "output_order": "lexicographic", "resumed_existing_work": args.resume_existing_work},
        "input_hash_verification": "passed",
        "output_hashes": output_hashes(output, outputs),
        "summary": summary,
    }
    (output / "OFFLINE_SCAN_RUN_MANIFEST.json").write_text(json.dumps(run_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

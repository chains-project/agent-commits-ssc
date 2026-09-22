"""Shared read-only helpers for bounded domestic-model message discovery."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator


VALID_SUPPLEMENT_AGENTS = {"codex", "copilot", "cursor"}
VALID_TIERS = {"main", "audit", "monthly_audit"}
BRANCHES = ("matched_10min", "supplementary_4h")


@dataclass(frozen=True)
class MessageRecord:
    branch: str
    repo_sha: str
    message: str
    month: str
    source_artifact: str
    source_line_number: int
    agent: str
    channel: str


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_rows(manifest: dict[str, Any], role: str) -> list[dict[str, Any]]:
    rows = [row for row in manifest["sources"] if row["logical_role"] == role]
    return sorted(rows, key=lambda row: row["resolved_path"])


def branch_paths(manifest: dict[str, Any], branch: str) -> tuple[Path, list[Path]]:
    if branch == "matched_10min":
        population_role = "matched_population"
        raw_roles = ("matched_raw_message", "matched_retry_raw_message")
    else:
        population_role = "supplement_channel_rows"
        raw_roles = ("supplement_raw_message",)
    population_path = Path(source_rows(manifest, population_role)[0]["resolved_path"])
    raw_paths = [Path(row["resolved_path"]) for role in raw_roles for row in source_rows(manifest, role)]
    return population_path, raw_paths


def valid_population_row(row: dict[str, str], branch: str) -> bool:
    if branch != "supplementary_4h":
        return True
    return row.get("agent") in VALID_SUPPLEMENT_AGENTS and row.get("tier") in VALID_TIERS


def load_population(path: Path, branch: str) -> tuple[set[str], int]:
    keys: set[str] = set()
    invalid = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        def valid_lines() -> Iterable[str]:
            nonlocal invalid
            for line in handle:
                if "\0" in line:
                    invalid += 1
                else:
                    yield line
        for row in csv.DictReader(valid_lines()):
            key = row.get("repo_sha", "")
            if key and valid_population_row(row, branch):
                keys.add(key)
    return keys, invalid


def message_from_envelope(branch: str, envelope: dict[str, Any], path: Path, line_no: int) -> MessageRecord:
    item = envelope.get("item") or {}
    commit = item.get("commit") or {}
    author = commit.get("author") or {}
    return MessageRecord(
        branch=branch,
        repo_sha=str(envelope.get("repo_sha") or ""),
        message=str(commit.get("message") or ""),
        month=str(author.get("date") or "")[:7] or "UNKNOWN",
        source_artifact=path.name,
        source_line_number=line_no,
        agent=str(envelope.get("agent") or ""),
        channel=str(envelope.get("channel") or ""),
    )


def iter_first_messages(branch: str, population: set[str], raw_paths: list[Path], stats: dict[str, int]) -> Iterator[MessageRecord]:
    seen: set[str] = set()
    for path in raw_paths:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                stats["raw_source_records_read"] += 1
                envelope = json.loads(line)
                repo_sha = str(envelope.get("repo_sha") or "")
                if repo_sha not in population:
                    continue
                stats["population_source_records"] += 1
                if repo_sha in seen:
                    stats["duplicate_population_source_records"] += 1
                    continue
                seen.add(repo_sha)
                yield message_from_envelope(branch, envelope, path, line_no)
    stats["unique_repository_sha_mapped"] = len(seen)
    stats["population_unique_repository_sha"] = len(population)
    stats["unmapped_repository_sha"] = len(population - seen)


def normalize_discovery_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def match_location(text: str, start: int) -> tuple[str, int]:
    line_index = text.count("\n", 0, start)
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    line = text[line_start:] if line_end < 0 else text[line_start:line_end]
    if line_index == 0:
        return "subject", line_index
    if re.match(r"^[A-Za-z][A-Za-z-]*:", line):
        return "trailer", line_index
    return "body", line_index


def redact_context(value: str) -> str:
    value = re.sub(r"(?i)https?://\S+", "<URL>", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+", "<EMAIL>", value)
    value = re.sub(r"(?<!\w)@[A-Za-z0-9_-]+", "<HANDLE>", value)
    value = re.sub(r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{12,40}(?![A-Fa-f0-9])", "<HASH>", value)
    value = re.sub(r"(?<!\d)\d{7,}(?!\d)", "<NUM>", value)
    return value


def limited_context(text: str, start: int, end: int, limit: int = 320) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    excerpt = redact_context(text[line_start:line_end].strip())
    excerpt = re.sub(r"\s+", " ", excerpt)
    return excerpt[:limit]


def context_cue(value: str) -> str:
    lower = value.casefold()
    attribution = ("co-authored-by", "generated by", "generated with", "assisted by", "authored by")
    structured = ("model:", "model=", "agent:", "agent=", "powered by", "using model")
    if any(cue in lower for cue in attribution):
        return "attribution_cue"
    if any(cue in lower for cue in structured):
        return "structured_cue"
    return "no_cue"


def normalized_template(context: str, family: str) -> str:
    value = context.casefold()
    value = re.sub(r"\b(?:v?\d+(?:\.\d+){0,3})\b", "<version>", value)
    value = re.sub(r"<url>|<email>|<handle>|<hash>|<num>", "<token>", value)
    value = re.sub(r"\s+", " ", value).strip()
    return f"{family}|{value}"


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    if not rows and fields is None:
        raise ValueError(f"No rows or fields for {path}")
    names = fields or list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def deterministic_gzip_writer(path: Path) -> tuple[io.TextIOWrapper, csv.DictWriter]:
    raw = path.open("wb")
    zipped = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
    text = io.TextIOWrapper(zipped, encoding="utf-8", newline="")
    return text, csv.DictWriter(text, fieldnames=[])


def json_dump(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

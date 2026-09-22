"""Shared local-only helpers for exploratory model observability E0-E3."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


BRANCH = "matched_10min"
LEVEL_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
GRANULARITY_ORDER = {"unknown": 0, "provider_only": 1, "model_family": 2, "exact_model": 3}


@dataclass(frozen=True)
class RawCommit:
    repo_sha: str
    repo: str
    sha: str
    message: str
    author_month: str
    author_login: str
    committer_login: str
    source_artifact: str
    source_line_number: int


@dataclass(frozen=True)
class CompiledPattern:
    signal_id: str
    provider: str
    model_family: str
    granularity: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class CompiledCue:
    cue_id: str
    candidate_level: str
    regex: re.Pattern[str]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def dump_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pseudonym(value: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}|{value}".encode("utf-8")).hexdigest()


def source_rows(manifest: dict[str, Any], role: str) -> list[dict[str, Any]]:
    rows = [row for row in manifest["sources"] if row["logical_role"] == role]
    return sorted(rows, key=lambda row: row["resolved_path"])


def matched_input_paths(manifest: dict[str, Any]) -> tuple[Path, list[Path]]:
    population = Path(source_rows(manifest, "matched_population")[0]["resolved_path"])
    roles = ("matched_raw_message", "matched_retry_raw_message")
    raw = [Path(row["resolved_path"]) for role in roles for row in source_rows(manifest, role)]
    return population, raw


def load_population_metadata(path: Path) -> dict[str, tuple[str, str, str, str]]:
    metadata: dict[str, tuple[str, str, str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            metadata[row["repo_sha"]] = (
                row["agent"],
                row["evidence_channels"],
                row["evidence_modes"],
                row["author_date"][:7],
            )
    return metadata


def raw_commit(envelope: dict[str, Any], path: Path, line_number: int) -> RawCommit:
    item = envelope.get("item") or {}
    commit = item.get("commit") or {}
    author = commit.get("author") or {}
    return RawCommit(
        repo_sha=str(envelope.get("repo_sha") or ""),
        repo=str(envelope.get("repo") or ""),
        sha=str(envelope.get("sha") or ""),
        message=str(commit.get("message") or ""),
        author_month=str(author.get("date") or "")[:7] or "UNKNOWN",
        author_login=str((item.get("author") or {}).get("login") or ""),
        committer_login=str((item.get("committer") or {}).get("login") or ""),
        source_artifact=path.name,
        source_line_number=line_number,
    )


def iter_first_raw_commits(population: set[str], raw_paths: list[Path]) -> Iterator[RawCommit]:
    seen: set[str] = set()
    for path in raw_paths:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                envelope = json.loads(line)
                key = str(envelope.get("repo_sha") or "")
                if key not in population or key in seen:
                    continue
                seen.add(key)
                yield raw_commit(envelope, path, line_number)


def compile_lexicon(payload: dict[str, Any]) -> tuple[list[CompiledPattern], list[CompiledCue], list[tuple[str, re.Pattern[str]]]]:
    patterns = [
        CompiledPattern(
            row["signal_id"], row["provider"], row["model_family"],
            row["granularity"], re.compile(row["pattern"]),
        )
        for row in payload["patterns"]
    ]
    cues = [
        CompiledCue(row["cue_id"], row["candidate_level"], re.compile(row["pattern"]))
        for row in payload["binding_cues"]
    ]
    exclusions = [(row["cue_id"], re.compile(row["pattern"])) for row in payload["exclusion_cues"]]
    return patterns, cues, exclusions


def redact(value: str) -> str:
    value = re.sub(r"(?i)https?://\S+", "<URL>", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+", "<EMAIL>", value)
    value = re.sub(r"(?<!\w)@[A-Za-z0-9_-]+", "<HANDLE>", value)
    value = re.sub(r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{12,40}(?![A-Fa-f0-9])", "<HASH>", value)
    return re.sub(r"\s+", " ", value).strip()


def bounded_line(line: str, limit: int = 480) -> str:
    return redact(unicodedata.normalize("NFKC", line))[:limit]


def best_granularity(values: set[str]) -> str:
    return max(values or {"unknown"}, key=lambda value: GRANULARITY_ORDER[value])


def line_evidence(line: str, patterns: list[CompiledPattern], cues: list[CompiledCue]) -> tuple[list[dict[str, str]], list[CompiledCue]]:
    hits = []
    for pattern in patterns:
        for match in pattern.regex.finditer(line):
            hits.append({
                "signal_id": pattern.signal_id,
                "provider": pattern.provider,
                "model_family": pattern.model_family,
                "granularity": pattern.granularity,
                "matched_text": match.group(0),
            })
    return hits, [cue for cue in cues if cue.regex.search(line)]


def classify_message(text: str, compiled: tuple[list[CompiledPattern], list[CompiledCue], list[tuple[str, re.Pattern[str]]]]) -> dict[str, Any]:
    patterns, cues, exclusions = compiled
    all_hits: list[dict[str, str]] = []
    cue_ids: set[str] = set()
    exclusion_ids: set[str] = set()
    best_lines: list[tuple[int, str]] = []
    level = "L0"
    for line in unicodedata.normalize("NFKC", text).splitlines() or [text]:
        hits, line_cues = line_evidence(line, patterns, cues)
        if not hits:
            continue
        all_hits.extend(hits)
        line_level = max((cue.candidate_level for cue in line_cues), default="L1", key=lambda value: LEVEL_ORDER[value])
        level = max((level, line_level), key=lambda value: LEVEL_ORDER[value])
        cue_ids.update(cue.cue_id for cue in line_cues)
        exclusion_ids.update(cue_id for cue_id, regex in exclusions if regex.search(line))
        best_lines.append((LEVEL_ORDER[line_level], bounded_line(line)))
    contexts = [line for _, line in sorted(best_lines, reverse=True)[:2]]
    exact = sorted({hit["matched_text"].casefold() for hit in all_hits if hit["granularity"] == "exact_model"})
    return {
        "sampling_stratum": level,
        "signal_ids": sorted({hit["signal_id"] for hit in all_hits}),
        "providers": sorted({hit["provider"] for hit in all_hits}),
        "model_families": sorted({hit["model_family"] for hit in all_hits if hit["model_family"] != "unknown"}),
        "exact_models": exact,
        "granularity": best_granularity({hit["granularity"] for hit in all_hits}),
        "binding_cues": sorted(cue_ids),
        "exclusion_cues": sorted(exclusion_ids),
        "limited_context": " || ".join(contexts)[:640],
    }


def write_csv(path: Path, rows: Iterator[dict[str, Any]] | list[dict[str, Any]], fields: list[str]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count

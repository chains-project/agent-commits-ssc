"""High-recall Tier 1 full-message scan after sealed baseline closure."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domestic_discovery_common import (
    BRANCHES,
    branch_paths,
    context_cue,
    iter_first_messages,
    json_dump,
    limited_context,
    load_json,
    load_population,
    match_location,
    normalize_discovery_text,
    normalized_template,
    sha256_file,
    stable_key,
    write_csv,
)


HIT_FIELDS = [
    "branch", "repo_sha_key", "family_id", "pattern_id", "canonical_name",
    "entity_type", "evidence_ceiling", "matched_span", "location", "line_index",
    "author_month", "context_cue", "template_key", "limited_context",
    "source_artifact", "source_line_number", "attribution_agent", "attribution_channel",
]


def compile_lexicon(lexicon: dict[str, Any]) -> list[tuple[dict[str, Any], re.Pattern[str]]]:
    return [(pattern, re.compile(pattern["regex"])) for pattern in lexicon["patterns"]]


def hit_row(record: Any, pattern: dict[str, Any], text: str, match: re.Match[str]) -> dict[str, Any]:
    location, line_index = match_location(text, match.start())
    context = limited_context(text, match.start(), match.end())
    template = normalized_template(context, pattern["family_id"])
    return {
        "branch": record.branch,
        "repo_sha_key": stable_key(record.repo_sha),
        "family_id": pattern["family_id"],
        "pattern_id": pattern["pattern_id"],
        "canonical_name": pattern["canonical_name"],
        "entity_type": pattern["entity_type"],
        "evidence_ceiling": pattern["evidence_ceiling"],
        "matched_span": match.group(0)[:80],
        "location": location,
        "line_index": line_index,
        "author_month": record.month,
        "context_cue": context_cue(context),
        "template_key": stable_key(template),
        "limited_context": context,
        "source_artifact": record.source_artifact,
        "source_line_number": record.source_line_number,
        "attribution_agent": record.agent,
        "attribution_channel": record.channel,
    }


def scan_branch(branch: str, manifest: dict[str, Any], compiled: list[Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    population_path, raw_paths = branch_paths(manifest, branch)
    population, invalid = load_population(population_path, branch)
    stats = defaultdict(int)
    stats["invalid_population_physical_rows"] = invalid
    hits: list[dict[str, Any]] = []
    for record in iter_first_messages(branch, population, raw_paths, stats):
        text = normalize_discovery_text(record.message)
        for pattern, regex in compiled:
            hits.extend(hit_row(record, pattern, text, match) for match in regex.finditer(text))
    return hits, dict(stats)


def merged_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched_keys = {row["repo_sha_key"] for row in hits if row["branch"] == "matched_10min"}
    return [
        row for row in hits
        if row["branch"] == "matched_10min" or row["repo_sha_key"] not in matched_keys
    ]


def aggregate(rows: list[dict[str, Any]], branch: str, level: str, key_field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row[key_field]].append(row)
    output = []
    for key, values in sorted(groups.items()):
        output.append({
            "branch": branch,
            "aggregation_level": level,
            "signal_key": key,
            "raw_occurrences": len(values),
            "unique_repository_sha": len({row["repo_sha_key"] for row in values}),
            "metric_definition": "regex spans in first authoritative full message per repository-SHA; merged prefers matched branch",
        })
    return output


def count_rows(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    branch_sets = {branch: [row for row in hits if row["branch"] == branch] for branch in BRANCHES}
    branch_sets["merged_expanded"] = merged_hits(hits)
    for branch, rows in branch_sets.items():
        output.extend(aggregate(rows, branch, "pattern", "pattern_id"))
        output.extend(aggregate(rows, branch, "family", "family_id"))
    return output


def representative(values: list[dict[str, Any]]) -> dict[str, Any]:
    cue_rank = {"attribution_cue": 0, "structured_cue": 1, "no_cue": 2}
    return sorted(values, key=lambda row: (cue_rank[row["context_cue"]], row["template_key"], row["pattern_id"]))[0]


def candidate_rows(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in hits:
        groups[(row["branch"], row["repo_sha_key"], row["family_id"])].append(row)
    output = []
    for _, values in sorted(groups.items()):
        chosen = representative(values)
        output.append({
            "branch": chosen["branch"],
            "repo_sha_key": chosen["repo_sha_key"],
            "family_id": chosen["family_id"],
            "pattern_ids": ";".join(sorted({row["pattern_id"] for row in values})),
            "entity_types": ";".join(sorted({row["entity_type"] for row in values})),
            "raw_occurrences": len(values),
            "location": chosen["location"],
            "author_month": chosen["author_month"],
            "context_cue": chosen["context_cue"],
            "template_key": chosen["template_key"],
            "limited_context": chosen["limited_context"],
            "source_artifact": chosen["source_artifact"],
            "source_line_number": chosen["source_line_number"],
        })
    return output


def template_rows(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        key = (row["family_id"], row["template_key"], row["location"], row["context_cue"])
        groups[key].append(row)
    output = []
    for key, values in sorted(groups.items()):
        output.append({
            "family_id": key[0],
            "template_key": key[1],
            "location": key[2],
            "context_cue": key[3],
            "candidate_repository_sha": len(values),
            "branches": ";".join(sorted({row["branch"] for row in values})),
            "example_limited_context": values[0]["limited_context"],
        })
    return output


def assert_baseline_gate(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"Sealed baseline closure is missing: {path}")
    closure = load_json(path)
    if closure["status"] != "closed" or closure["input_hash_verification"] != "passed":
        raise RuntimeError("Sealed baseline is not closed")
    return closure


def output_hashes(output: Path, names: list[str]) -> dict[str, str]:
    return {name: sha256_file(output / name) for name in names}


def run(root: Path, output: Path, baseline_closure: Path) -> dict[str, Any]:
    closure = assert_baseline_gate(baseline_closure)
    manifest = load_json(root / "INPUT_FREEZE_MANIFEST.json")
    lexicon_path = root / "DOMESTIC_MODEL_DISCOVERY_LEXICON.v0.1.json"
    lexicon = load_json(lexicon_path)
    compiled = compile_lexicon(lexicon)
    all_hits: list[dict[str, Any]] = []
    branch_stats: dict[str, Any] = {}
    for branch in BRANCHES:
        hits, branch_stats[branch] = scan_branch(branch, manifest, compiled)
        all_hits.extend(hits)
    counts = count_rows(all_hits)
    candidates = candidate_rows(all_hits)
    templates = template_rows(candidates)
    names = [
        "DOMESTIC_MODEL_KEYWORD_COUNTS.csv",
        "DOMESTIC_MODEL_DISCOVERY_LEDGER.csv",
        "DOMESTIC_MODEL_DISCOVERY_CANDIDATES.csv",
        "DOMESTIC_MODEL_TEMPLATE_CATALOG.csv",
    ]
    write_csv(output / names[0], counts)
    write_csv(output / names[1], all_hits, HIT_FIELDS)
    write_csv(output / names[2], candidates)
    write_csv(output / names[3], templates)
    summary = {
        "schema_version": "domestic-model-keyword-scan-v1",
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "complete",
        "baseline_gate": closure["status"],
        "lexicon_version": lexicon["lexicon_version"],
        "lexicon_sha256": sha256_file(lexicon_path),
        "branch_mapping": branch_stats,
        "ledger_occurrence_rows": len(all_hits),
        "candidate_branch_repository_family_rows": len(candidates),
        "template_rows": len(templates),
        "full_raw_messages_persisted": 0,
        "output_hashes": output_hashes(output, names),
        "interpretation": "High-recall name/context discovery only; not authorship, use, or model-execution evidence.",
    }
    json_dump(output / "DOMESTIC_MODEL_SCAN_SUMMARY.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-closure", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(
        run(args.root.resolve(), args.output.resolve(), args.baseline_closure.resolve()),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()

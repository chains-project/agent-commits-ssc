"""Fast deterministic incremental scan for the network-verified Qwen H1 trailer."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import offline_signal_scanner as base


ROOT = Path(__file__).resolve().parent
NEEDLE = b"qwen-coder@alibabacloud.com"


def scan_branch(branch: str, population: set[str], paths: list[Path], regex: re.Pattern[str]) -> dict[str, dict[str, object]]:
    hits: dict[str, dict[str, object]] = {}
    for path in paths:
        with gzip.open(path, "rb") as handle:
            for line_number, raw in enumerate(handle, 1):
                if NEEDLE not in raw.lower():
                    continue
                envelope = json.loads(raw)
                repo_sha = str(envelope.get("repo_sha") or "")
                if repo_sha not in population:
                    continue
                fields, trailers, month = base.extract_fields(envelope)
                if not any(regex.search(line) for line in trailers):
                    continue
                row = hits.setdefault(repo_sha, {
                    "branch": branch,
                    "repo_sha_key": base.stable_key(repo_sha),
                    "author_month": month,
                    "source_artifacts": set(),
                    "source_row_keys": set(),
                    "agents": set(),
                    "channels": set(),
                })
                row["source_artifacts"].add(path.name)
                row["source_row_keys"].add(base.stable_key(f"{branch}|{path.name}|{line_number}|{repo_sha}"))
                row["agents"].add(str(envelope.get("agent") or ""))
                row["channels"].add(str(envelope.get("channel") or ""))
    return hits


def serialize_hits(hit_maps: list[dict[str, dict[str, object]]], target: Path) -> None:
    fields = ["branch", "repo_sha_key", "signal_id", "field", "evidence_tier", "author_month", "source_artifacts", "source_row_keys", "attribution_agents", "attribution_channels"]
    rows = []
    for hit_map in hit_maps:
        for row in hit_map.values():
            rows.append({
                "branch": row["branch"], "repo_sha_key": row["repo_sha_key"],
                "signal_id": "h1_qwen_code_coauthor_trailer", "field": "trailer_line",
                "evidence_tier": "H1", "author_month": row["author_month"],
                "source_artifacts": ";".join(sorted(row["source_artifacts"])),
                "source_row_keys": ";".join(sorted(row["source_row_keys"])),
                "attribution_agents": ";".join(sorted(row["agents"])),
                "attribution_channels": ";".join(sorted(row["channels"])),
            })
    rows.sort(key=lambda row: (row["branch"], row["repo_sha_key"]))
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=ROOT / "SIGNATURE_REGISTRY.v1.1.0.json")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest_path = args.input_manifest.resolve()
    registry_path = args.registry.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = base.load_json(manifest_path)
    registry = base.load_json(registry_path)
    pattern = next(row for row in registry["patterns"] if row["signal_id"] == "h1_qwen_code_coauthor_trailer")
    regex = re.compile(pattern["pattern"])
    matched, _ = base.load_population(Path(base.source_rows(manifest, "matched_population")[0]["resolved_path"]), "matched_10min")
    supplement, _ = base.load_population(Path(base.source_rows(manifest, "supplement_channel_rows")[0]["resolved_path"]), "supplementary_4h")
    matched_paths = [Path(row["resolved_path"]) for role in ("matched_raw_message", "matched_retry_raw_message") for row in base.source_rows(manifest, role)]
    supplement_paths = [Path(row["resolved_path"]) for row in base.source_rows(manifest, "supplement_raw_message")]
    matched_hits = scan_branch("matched_10min", set(matched), matched_paths, regex)
    supplement_hits = scan_branch("supplementary_4h", set(supplement), supplement_paths, regex)
    ledger = output_dir / "QWEN_H1_INCREMENTAL_HITS.csv"
    serialize_hits([matched_hits, supplement_hits], ledger)
    merged = set(matched_hits) | set(supplement_hits)
    summary = {
        "schema_version": "qwen-h1-incremental-v1.0",
        "signal_id": pattern["signal_id"],
        "counts_unique_repository_sha": {
            "matched_10min": len(matched_hits),
            "supplementary_4h": len(supplement_hits),
            "merged_expanded": len(merged),
        },
        "interpretation": "Producer-documented exact H1 hits in the frozen selected corpus; not prevalence or recall",
    }
    summary_path = output_dir / "QWEN_H1_INCREMENTAL_SUMMARY.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    run = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "scanner_sha256": base.sha256_file(Path(__file__)),
        "input_manifest_sha256": base.sha256_file(manifest_path),
        "registry_sha256": base.sha256_file(registry_path),
        "output_hashes": {
            ledger.name: base.sha256_file(ledger),
            summary_path.name: base.sha256_file(summary_path),
        },
        "optimization": "byte prefilter followed by JSON parse, authoritative population filter, exact trailer regex and repository-SHA dedup",
    }
    (output_dir / "QWEN_H1_INCREMENTAL_RUN_MANIFEST.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

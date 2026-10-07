"""E1: deterministic message-feature census over the frozen matched frame."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from exploratory_observability_common import (
    classify_message,
    compile_lexicon,
    dump_json,
    iter_first_raw_commits,
    load_json,
    load_population_metadata,
    matched_input_paths,
    pseudonym,
    sha256_file,
)


FRAME_FIELDS = [
    "repo_sha_key", "repo_key", "developer_key", "author_login_available",
    "author_month", "evidence_agent", "evidence_channels", "evidence_modes",
    "sampling_stratum", "signal_count", "signal_ids", "providers",
    "model_families", "exact_models", "granularity", "binding_cues",
    "exclusion_cues", "limited_context", "source_artifact", "source_line_number",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def join(values: list[str]) -> str:
    return ";".join(values)


def make_row(commit, meta, evidence, salt: str) -> dict[str, object]:
    agent, channels, modes, population_month = meta
    login = commit.author_login or commit.committer_login
    return {
        "repo_sha_key": pseudonym(commit.repo_sha, salt),
        "repo_key": pseudonym(commit.repo, salt),
        "developer_key": pseudonym(login, salt) if login else "",
        "author_login_available": str(bool(login)).lower(),
        "author_month": commit.author_month or population_month,
        "evidence_agent": agent,
        "evidence_channels": channels,
        "evidence_modes": modes,
        "sampling_stratum": evidence["sampling_stratum"],
        "signal_count": len(evidence["signal_ids"]),
        "signal_ids": join(evidence["signal_ids"]),
        "providers": join(evidence["providers"]),
        "model_families": join(evidence["model_families"]),
        "exact_models": join(evidence["exact_models"]),
        "granularity": evidence["granularity"],
        "binding_cues": join(evidence["binding_cues"]),
        "exclusion_cues": join(evidence["exclusion_cues"]),
        "limited_context": evidence["limited_context"],
        "source_artifact": commit.source_artifact,
        "source_line_number": commit.source_line_number,
    }


class CensusStats:
    def __init__(self) -> None:
        self.counts: Counter[tuple[str, str]] = Counter()
        self.all_repos: set[str] = set()
        self.observable_repos: set[str] = set()
        self.exact_repos: set[str] = set()
        self.all_devs: set[str] = set()
        self.observable_devs: set[str] = set()
        self.exact_devs: set[str] = set()

    def add(self, row: dict[str, object]) -> None:
        stratum = str(row["sampling_stratum"])
        self.counts[("sampling_stratum", stratum)] += 1
        self.counts[("granularity", str(row["granularity"]))] += 1
        for family in filter(None, str(row["model_families"]).split(";")):
            self.counts[("model_family", family)] += 1
        for provider in filter(None, str(row["providers"]).split(";")):
            self.counts[("provider", provider)] += 1
        for signal in filter(None, str(row["signal_ids"]).split(";")):
            self.counts[("signal", signal)] += 1
        self._add_units(row)

    def _add_units(self, row: dict[str, object]) -> None:
        repo = str(row["repo_key"])
        dev = str(row["developer_key"])
        observable = row["sampling_stratum"] != "L0"
        exact = row["granularity"] == "exact_model"
        self.all_repos.add(repo)
        if dev:
            self.all_devs.add(dev)
        if observable:
            self.observable_repos.add(repo)
            if dev:
                self.observable_devs.add(dev)
        if exact:
            self.exact_repos.add(repo)
            if dev:
                self.exact_devs.add(dev)


def count_rows(stats: CensusStats, denominator: int) -> list[dict[str, object]]:
    rows = []
    for (dimension, key), count in sorted(stats.counts.items()):
        rows.append(metric("commit", dimension, key, count, denominator))
    rows.extend([
        metric("repository", "observable", "L1-L3", len(stats.observable_repos), len(stats.all_repos)),
        metric("repository", "exact_identifiable", "exact_model", len(stats.exact_repos), len(stats.all_repos)),
        metric("developer", "observable", "L1-L3", len(stats.observable_devs), len(stats.all_devs)),
        metric("developer", "exact_identifiable", "exact_model", len(stats.exact_devs), len(stats.all_devs)),
    ])
    return rows


def metric(unit: str, dimension: str, key: str, numerator: int, denominator: int) -> dict[str, object]:
    return {
        "branch": "matched_10min", "unit": unit, "dimension": dimension,
        "key": key, "numerator": numerator, "denominator": denominator,
        "observable_rate": numerator / denominator if denominator else 0,
        "interpretation": "deterministic proxy-signal census; not actual model use",
    }


def run(config_dir: Path, run_dir: Path) -> dict[str, object]:
    freeze = load_json(run_dir / "EXPLORATORY_E0_FREEZE.json")
    manifest = load_json(config_dir / "INPUT_FREEZE_MANIFEST.json")
    lexicon = load_json(config_dir / "EXPLORATORY_MODEL_PROXY_LEXICON.v1.json")
    compiled = compile_lexicon(lexicon)
    population_path, raw_paths = matched_input_paths(manifest)
    metadata = load_population_metadata(population_path)
    stats = CensusStats()
    mapped = 0
    frame_path = run_dir / "EXPLORATORY_LOCAL_COMMIT_FRAME.csv"
    candidates_path = run_dir / "EXPLORATORY_MODEL_CANDIDATES.csv"
    with frame_path.open("w", encoding="utf-8", newline="") as frame_handle, candidates_path.open("w", encoding="utf-8", newline="") as candidate_handle:
        frame_writer = csv.DictWriter(frame_handle, fieldnames=FRAME_FIELDS, lineterminator="\n")
        candidate_writer = csv.DictWriter(candidate_handle, fieldnames=FRAME_FIELDS, lineterminator="\n")
        frame_writer.writeheader()
        candidate_writer.writeheader()
        for commit in iter_first_raw_commits(set(metadata), raw_paths):
            evidence = classify_message(commit.message, compiled)
            row = make_row(commit, metadata[commit.repo_sha], evidence, freeze["pseudonymization_salt"])
            frame_writer.writerow(row)
            if row["sampling_stratum"] != "L0":
                candidate_writer.writerow(row)
            stats.add(row)
            mapped += 1
    counts = count_rows(stats, len(metadata))
    counts_path = run_dir / "EXPLORATORY_LOCAL_SIGNAL_COUNTS.csv"
    with counts_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(counts[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(counts)
    summary = {
        "schema_version": "exploratory-local-census-v1",
        "created_utc": utc_now(),
        "status": "complete" if mapped == len(metadata) else "failed",
        "branch": "matched_10min",
        "population_rows": len(metadata),
        "mapped_rows": mapped,
        "unmapped_rows": len(metadata) - mapped,
        "candidate_rows": sum(value for (dimension, key), value in stats.counts.items() if dimension == "sampling_stratum" and key != "L0"),
        "full_raw_messages_persisted": 0,
        "frame_sha256": sha256_file(frame_path),
        "candidates_sha256": sha256_file(candidates_path),
        "counts_sha256": sha256_file(counts_path),
    }
    dump_json(run_dir / "EXPLORATORY_LOCAL_CENSUS_MANIFEST.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.config_dir, args.run_dir)
    print(result)
    raise SystemExit(0 if result["status"] == "complete" else 1)


if __name__ == "__main__":
    main()

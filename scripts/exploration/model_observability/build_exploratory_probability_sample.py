"""E3: deterministic equal-allocation probability sample from the E1 frame."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path

from exploratory_observability_common import bounded_line, dump_json, load_json, pseudonym, sha256_file


STRATA = ("L3", "L2", "L1", "L0")
PER_STRATUM = 30


def priority(seed: str, key: str) -> int:
    return int(hashlib.sha256(f"{seed}|{key}".encode()).hexdigest(), 16)


def cluster_key(row: dict[str, str]) -> str:
    # Missing public login must not collapse unrelated commits into one cluster.
    return row["developer_key"] or f"missing-login:{row['repo_sha_key']}"


def select_rows(frame: Path, seed: str, per_stratum: int = PER_STRATUM):
    counts: Counter[str] = Counter()
    cluster_sizes: Counter[tuple[str, str]] = Counter()
    representatives: dict[tuple[str, str], tuple[int, dict[str, str]]] = {}
    with frame.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            stratum = row["sampling_stratum"]
            counts[stratum] += 1
            cluster = cluster_key(row)
            key = (stratum, cluster)
            cluster_sizes[key] += 1
            score = priority(f"{seed}|within-cluster", row["repo_sha_key"])
            if key not in representatives or score < representatives[key][0]:
                representatives[key] = (score, row)
    sparse_caps = {}
    for stratum in STRATA:
        sizes = [size for (level, _), size in cluster_sizes.items() if level == stratum]
        if len(sizes) < per_stratum:
            feasible = [cap for cap in range(1, per_stratum + 1) if sum(min(cap, size) for size in sizes) <= per_stratum]
            sparse_caps[stratum] = max(feasible)
    sparse_members: dict[tuple[str, str], list[tuple[int, dict[str, str]]]] = {}
    if sparse_caps:
        with frame.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                stratum = row["sampling_stratum"]
                if stratum not in sparse_caps:
                    continue
                key = (stratum, cluster_key(row))
                sparse_members.setdefault(key, []).append((priority(f"{seed}|within-cluster", row["repo_sha_key"]), row))
    selected = []
    for stratum in STRATA:
        candidates = [(priority(f"{seed}|cluster", key[1]), key, item[1]) for key, item in representatives.items() if key[0] == stratum]
        candidates.sort(key=lambda item: (item[0], item[1]))
        cluster_N = len(candidates)
        if cluster_N >= per_stratum:
            rows = [item[2] for item in candidates[:per_stratum]]
        else:
            cap = sparse_caps[stratum]
            rows = []
            for _, key, _ in candidates:
                rows.extend(row for _, row in sorted(sparse_members[key])[:cap])
        for row in rows:
            key = (stratum, cluster_key(row))
            row["selection_priority"] = str(priority(f"{seed}|cluster", key[1]))
            row["cluster_N"] = str(cluster_N)
            row["cluster_n"] = str(min(per_stratum, cluster_N))
            row["within_cluster_N"] = str(cluster_sizes[key])
            row["within_cluster_n"] = str(1 if cluster_N >= per_stratum else min(sparse_caps[stratum], cluster_sizes[key]))
            selected.append(row)
    selected.sort(key=lambda r: (STRATA.index(r["sampling_stratum"]), int(r["selection_priority"])))
    return counts, selected


def hydrate_context(selected: list[dict[str, str]], raw_paths: list[Path], salt: str) -> None:
    wanted = {(row["source_artifact"], int(row["source_line_number"])): row for row in selected}
    paths: dict[str, list[Path]] = {}
    for path in raw_paths:
        paths.setdefault(path.name, []).append(path)
    for (artifact, line_number), row in wanted.items():
        if row["limited_context"]:
            row["message_context"] = row["limited_context"]
            continue
        for path in paths[artifact]:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for current, line in enumerate(handle, 1):
                    if current != line_number:
                        continue
                    envelope = json.loads(line)
                    if pseudonym(str(envelope.get("repo_sha") or ""), salt) != row["repo_sha_key"]:
                        break
                    message = str(((envelope.get("item") or {}).get("commit") or {}).get("message") or "")
                    row["message_context"] = bounded_line(message, 640)
                    break
            if row.get("message_context"):
                break


def run(run_dir: Path) -> dict:
    freeze = load_json(run_dir / "EXPLORATORY_E0_FREEZE.json")
    frame = run_dir / "EXPLORATORY_LOCAL_COMMIT_FRAME.csv"
    counts, selected = select_rows(frame, freeze["sampling_seed"])
    raw_paths = [Path(row["resolved_path"]) for row in freeze["inputs"] if row["logical_role"] in {"matched_raw_message", "matched_retry_raw_message"}]
    hydrate_context(selected, raw_paths, freeze["pseudonymization_salt"])
    repo_counts = Counter(row["repo_key"] for row in selected)
    dev_counts = Counter(row["developer_key"] for row in selected if row["developer_key"])
    for rank, row in enumerate(selected, 1):
        stratum = row["sampling_stratum"]
        row["selection_rank"] = rank
        row["stratum_N"] = counts[stratum]
        row["stratum_n"] = min(PER_STRATUM, counts[stratum])
        cluster_probability = int(row["cluster_n"]) / int(row["cluster_N"])
        within_probability = int(row["within_cluster_n"]) / int(row["within_cluster_N"])
        row["inclusion_probability"] = cluster_probability * within_probability
        row["base_weight"] = 1 / row["inclusion_probability"]
        row.pop("limited_context", None)
    path = run_dir / "EXPLORATORY_PROBABILITY_SAMPLE.csv"
    fields = ["selection_rank", *[k for k in selected[0] if k != "selection_rank"]]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(selected)
    sparse_caps = {}
    for stratum in STRATA:
        rows = [row for row in selected if row["sampling_stratum"] == stratum and int(row["cluster_N"]) < PER_STRATUM]
        if rows:
            sparse_caps[stratum] = max(int(row["within_cluster_n"]) for row in rows)
    result = {
        "schema_version": "exploratory-sample-v1", "status": "complete",
        "design": "two-stage probability sample: developer clusters within stratum, then bounded commit sample within cluster",
        "sparse_stratum_cluster_caps": sparse_caps,
        "stratum_counts": dict(counts), "stratum_sample_counts": dict(Counter(r["sampling_stratum"] for r in selected)),
        "sample_rows": len(selected), "absolute_cap": 120,
        "max_cases_per_repository": max(repo_counts.values(), default=0),
        "max_cases_per_developer": max(dev_counts.values(), default=0),
        "sample_sha256": sha256_file(path), "full_raw_messages_persisted": 0,
    }
    dump_json(run_dir / "EXPLORATORY_SAMPLE_MANIFEST.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--run-dir", type=Path, required=True)
    print(run(parser.parse_args().run_dir))

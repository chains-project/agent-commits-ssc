"""Build a deterministic stratified discovery-annotation sample capped at 240."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domestic_discovery_common import json_dump, sha256_file, stable_key, write_csv


ABSOLUTE_CAP = 240
DEFAULT_SEED = "domestic-tier1-discovery-2026-09-03-v1"
ANNOTATION_FIELDS = [
    "operational_attribution_statement",
    "structured_harness_model_self_report",
    "provider_api_model_dependency_integration",
    "documentation_benchmark_test_changelog_release",
    "ordinary_discussion_or_unrelated",
    "ambiguous",
    "not_reviewable",
]


def read_candidates(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def template_counts(rows: list[dict[str, str]]) -> Counter[tuple[str, str]]:
    return Counter((row["family_id"], row["template_key"]) for row in rows)


def frequency_band(count: int) -> str:
    if count <= 20:
        return "rare_le_20"
    if count <= 100:
        return "medium_21_100"
    return "common_gt_100"


def add_strata(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    frequencies = template_counts(rows)
    output = []
    for row in rows:
        item = dict(row)
        count = frequencies[(row["family_id"], row["template_key"])]
        item["template_frequency"] = str(count)
        item["frequency_band"] = frequency_band(count)
        parts = [row["family_id"], row["branch"], row["author_month"], row["location"], row["context_cue"], item["frequency_band"]]
        item["stratum_id"] = "|".join(parts)
        output.append(item)
    return output


def row_priority(row: dict[str, str], seed: str) -> str:
    return stable_key(f"{seed}|{row['repo_sha_key']}|{row['family_id']}|{row['template_key']}")


def stratum_priority(stratum: str) -> tuple[int, int, str]:
    cue = stratum.split("|")[-2]
    band = stratum.split("|")[-1]
    cue_rank = {"attribution_cue": 0, "structured_cue": 1, "no_cue": 2}[cue]
    band_rank = {"rare_le_20": 0, "medium_21_100": 1, "common_gt_100": 2}[band]
    return cue_rank, band_rank, stratum


def build_queues(rows: list[dict[str, str]], seed: str) -> dict[str, deque[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row["stratum_id"]].append(row)
    return {
        key: deque(sorted(values, key=lambda row: row_priority(row, seed)))
        for key, values in groups.items()
    }


def family_strata(queues: dict[str, deque[dict[str, str]]]) -> dict[str, deque[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for stratum in queues:
        grouped[stratum.split("|", 1)[0]].append(stratum)
    return {family: deque(sorted(values, key=stratum_priority)) for family, values in grouped.items()}


def deterministic_select(rows: list[dict[str, str]], seed: str, cap: int = ABSOLUTE_CAP) -> list[dict[str, str]]:
    if cap > ABSOLUTE_CAP:
        raise ValueError(f"cap exceeds absolute authorization limit: {cap}")
    queues = build_queues(rows, seed)
    by_family = family_strata(queues)
    selected: list[dict[str, str]] = []
    while len(selected) < min(cap, len(rows)):
        progressed = False
        for family in sorted(by_family):
            strata = by_family[family]
            if not strata:
                continue
            for _ in range(len(strata)):
                stratum = strata[0]
                strata.rotate(-1)
                if queues[stratum]:
                    selected.append(queues[stratum].popleft())
                    progressed = True
                    break
            if len(selected) == cap:
                break
        if not progressed:
            break
    return selected


def sample_rows(selected: list[dict[str, str]], eligible_counts: Counter[str]) -> list[dict[str, Any]]:
    output = []
    for rank, row in enumerate(selected, 1):
        item: dict[str, Any] = {"selection_rank": rank}
        item.update(row)
        item["stratum_eligible"] = eligible_counts[row["stratum_id"]]
        item["discovery_annotation"] = ""
        item["annotation_rationale"] = ""
        output.append(item)
    return output


def allocation_rows(rows: list[dict[str, str]], selected: list[dict[str, str]]) -> list[dict[str, Any]]:
    eligible = Counter(row["stratum_id"] for row in rows)
    chosen = Counter(row["stratum_id"] for row in selected)
    return [
        {
            "stratum_id": key,
            "eligible_rows": eligible[key],
            "selected_rows": chosen[key],
            "uncovered_rows": eligible[key] - chosen[key],
        }
        for key in sorted(eligible)
    ]


def run(output: Path, seed: str, cap: int) -> dict[str, Any]:
    source = output / "DOMESTIC_MODEL_DISCOVERY_CANDIDATES.csv"
    rows = add_strata(read_candidates(source))
    selected = deterministic_select(rows, seed, cap)
    eligible_counts = Counter(row["stratum_id"] for row in rows)
    sample = sample_rows(selected, eligible_counts)
    allocation = allocation_rows(rows, selected)
    sample_path = output / "DOMESTIC_MODEL_DISCOVERY_SAMPLE.csv"
    allocation_path = output / "DOMESTIC_MODEL_DISCOVERY_SAMPLE_ALLOCATION.csv"
    write_csv(sample_path, sample)
    write_csv(allocation_path, allocation)
    manifest = {
        "schema_version": "domestic-model-discovery-sample-v1",
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "seed": seed,
        "absolute_authorized_cap": ABSOLUTE_CAP,
        "requested_cap": cap,
        "eligible_rows": len(rows),
        "selected_rows": len(selected),
        "uncovered_rows": len(rows) - len(selected),
        "annotation_status": "pending_agent_discovery_annotation",
        "terminology": "Report selected context counts and proportions only.",
        "source_sha256": sha256_file(source),
        "sample_sha256": sha256_file(sample_path),
        "allocation_sha256": sha256_file(allocation_path),
    }
    json_dump(output / "DOMESTIC_MODEL_DISCOVERY_SAMPLE_MANIFEST.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--cap", type=int, default=ABSOLUTE_CAP)
    args = parser.parse_args()
    print(json.dumps(run(args.output.resolve(), args.seed, args.cap), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

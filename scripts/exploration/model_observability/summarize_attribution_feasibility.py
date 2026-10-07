"""Recount frozen sample labels and coverage; no inference or corpus scanning."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def load_ranked(path: Path) -> dict[int, dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    keyed = {int(row["selection_rank"]): row for row in rows}
    if len(keyed) != len(rows):
        raise ValueError(f"Duplicate selection_rank in {path.name}")
    return keyed


def validate_inputs(sample: dict, consensus: dict) -> None:
    ranks = set(range(1, 121))
    if set(sample) != ranks or set(consensus) != ranks:
        raise ValueError("Both tables must contain exactly ranks 1 through 120")
    if Counter(r["sampling_stratum"] for r in sample.values()) != dict.fromkeys(("L3", "L2", "L1", "L0"), 30):
        raise ValueError("Expected 30 samples per frozen stratum")
    for rank in ranks:
        item, code = sample[rank], consensus[rank]
        if item["diff_status"] not in {"ok", "error", "not_in_local_index"}:
            raise ValueError("Unknown diff availability status")
        if item["diff_available"] != ("true" if item["diff_status"] == "ok" else "false"):
            raise ValueError("Inconsistent diff availability fields")
        if code["x_level"] not in {"X0", "X2", "X3", "XC"}:
            raise ValueError("Unexpected frozen consensus label")
        if code["consensus_rule"] == "disagreement_to_XC" and code["x_level"] != "XC":
            raise ValueError("A coding disagreement must remain XC")


def tally(rows: list[dict], field: str) -> dict:
    return dict(sorted(Counter(row[field] for row in rows).items()))


def summarize(sample: dict, consensus: dict) -> dict:
    validate_inputs(sample, consensus)
    codes = list(consensus.values())
    positives = [r for r in codes if r["x_level"] in {"X2", "X3"}]
    sources = [set(r["source_type"].split(";")) for r in positives]
    message_only = sum(bool(s) and s <= {"commit_message", "commit_trailer"} for s in sources)
    statuses = tally(list(sample.values()), "diff_status")
    return {
        "sample_rows": len(sample),
        "labels": tally(codes, "x_level"),
        "labels_by_stratum": {
            h: tally([consensus[k] for k in sample if sample[k]["sampling_stratum"] == h], "x_level")
            for h in ("L3", "L2", "L1", "L0")
        },
        "positive_rows": len(positives),
        "positive_granularity": tally(positives, "granularity"),
        "positive_model_families": tally(positives, "model_family"),
        "positive_source_types": tally(positives, "source_type"),
        "positives_supported_only_by_message_or_trailer": message_only,
        "positives_citing_other_sources": len(positives) - message_only,
        "diff_status_counts": statuses,
        "diff_index_matches": len(sample) - statuses.get("not_in_local_index", 0),
        # Frozen index status, not a fresh check of external artifact readability.
        "readable_diffs": statuses.get("ok", 0),
    }


def verify(root: Path) -> dict:
    data = root / "data" / "model_observability"
    actual = summarize(
        load_ranked(data / "EXPLORATORY_PROBABILITY_SAMPLE.csv"),
        load_ranked(data / "EXPLORATORY_AGENT_CONSENSUS.csv"),
    )
    expected_path = root / "results/model_observability/EXPLORATORY_ATTRIBUTION_FEASIBILITY_SUMMARY.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    if actual != expected["sample_counts"]:
        raise ValueError("Recomputed sample counts differ from the published summary")
    return {"status": "exact_match", "sample_counts": actual}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    print(json.dumps(verify(parser.parse_args().root), indent=2))

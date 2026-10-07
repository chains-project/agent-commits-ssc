"""E3 descriptive QA for two independent agent coding files and frozen recodes."""

from __future__ import annotations

import argparse
import csv
import math
from collections import Counter
from pathlib import Path

from exploratory_observability_common import dump_json, sha256_file

FIELDS = ("x_level", "granularity", "provider", "model_family", "exact_model")

def read_codes(path: Path, expected: set[int]) -> dict[int, dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = {}
        for row in csv.DictReader(handle):
            rank = int(row["selection_rank"])
            if rank in rows:
                raise ValueError(f"{path.name}: duplicate selection_rank {rank}")
            rows[rank] = row
    if set(rows) != expected:
        raise ValueError(f"{path.name}: expected ranks {sorted(expected)}, got {sorted(rows)}")
    return rows

def kappa(a: list[str], b: list[str]) -> float | None:
    n = len(a)
    if not n: return None
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return None if math.isclose(pe, 1) else (po - pe) / (1 - pe)

def stability(initial: dict[int, dict[str, str]], recode: dict[int, dict[str, str]]) -> dict:
    exact = sum(all(initial[r][f] == row[f] for f in FIELDS) for r, row in recode.items())
    return {"rows": len(recode), "exact_agreement_rows": exact, "exact_agreement_rate": exact / len(recode)}

def run(run_dir: Path) -> dict:
    ranks = set(range(1, 121))
    a = read_codes(run_dir / "EXPLORATORY_AGENT_A_CODES.csv", ranks)
    b = read_codes(run_dir / "EXPLORATORY_AGENT_B_CODES.csv", ranks)
    recode_ranks = set()
    with (run_dir / "EXPLORATORY_AGENT_RECODE_PACKAGE.csv").open("r", encoding="utf-8", newline="") as handle:
        recode_ranks = {int(row["selection_rank"]) for row in csv.DictReader(handle)}
    ar = read_codes(run_dir / "EXPLORATORY_AGENT_A_RECODE.csv", recode_ranks)
    br = read_codes(run_dir / "EXPLORATORY_AGENT_B_RECODE.csv", recode_ranks)
    consensus = []
    agreements = 0
    for rank in sorted(ranks):
        exact = all(a[rank][f] == b[rank][f] for f in FIELDS)
        if exact:
            agreements += 1
            row = {f: a[rank][f] for f in FIELDS}
            row.update({"source_type": a[rank]["source_type"], "binding_assumption": a[rank]["binding_assumption"], "rationale": a[rank]["rationale"], "consensus_rule": "exact_agent_agreement"})
        else:
            row = {"x_level": "XC", "granularity": "unknown", "provider": "uncertain", "model_family": "uncertain", "exact_model": "uncertain", "source_type": "uncertain", "binding_assumption": "none", "rationale": "Agent labels disagreed; no adjudication under frozen rule.", "consensus_rule": "disagreement_to_XC"}
        consensus.append({"selection_rank": rank, **row})
    path = run_dir / "EXPLORATORY_AGENT_CONSENSUS.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(consensus[0]), lineterminator="\n"); writer.writeheader(); writer.writerows(consensus)
    result = {"schema_version": "exploratory-agent-coding-qa-v1", "status": "complete", "rows": 120, "exact_label_agreement_rows": agreements, "exact_label_agreement_rate": agreements / 120, "x_level_raw_agreement": sum(a[r]["x_level"] == b[r]["x_level"] for r in ranks) / 120, "x_level_cohen_kappa": kappa([a[r]["x_level"] for r in sorted(ranks)], [b[r]["x_level"] for r in sorted(ranks)]), "agent_a_recode_stability": stability(a, ar), "agent_b_recode_stability": stability(b, br), "consensus_x_level_counts": dict(Counter(r["x_level"] for r in consensus)), "interpretation": "agent-coded discovery annotation process consistency", "consensus_sha256": sha256_file(path)}
    dump_json(run_dir / "EXPLORATORY_AGENT_CODING_QA.json", result)
    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--run-dir", type=Path, required=True); print(run(parser.parse_args().run_dir))

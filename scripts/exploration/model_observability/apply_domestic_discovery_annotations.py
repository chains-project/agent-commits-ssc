"""Apply the agent's row-by-row bounded-context discovery annotations."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domestic_discovery_common import json_dump, sha256_file, write_csv


EXPECTED_SAMPLE_SHA256 = "0e1208cdf5451c600542e2d8dc68ed4a80a3c6bb92336e6a98fd6575343b4186"


GROUPS = {
    "operational_attribution_statement": [
        1, 2, 3, 4, 5, 6, 8, 9, 10, 13, 14, 15, 16, 18, 19, 20, 21,
        23, 24, 25, 28, 29, 30, 31, 33, 35, 38, 40, 41, 43, 45, 48, 50,
        53, 55, 58, 60, 63, 65, 68, 70, 73, 75, 78, 139, 158, 181, 230, 234,
    ],
    "structured_harness_model_self_report": [39, 49, 93, 105, 125],
    "documentation_benchmark_test_changelog_release": [
        36, 77, 107, 109, 113, 117, 121, 124, 128, 153, 155, 161, 171,
        173, 185, 186, 210, 215, 221, 225,
    ],
    "ordinary_discussion_or_unrelated": [
        37, 62, 67, 74, 99, 104, 149, 156, 157, 165, 167, 194, 212, 213,
    ],
    "ambiguous": [11, 112, 134, 166, 192],
    "not_reviewable": [22, 32, 42, 59, 92, 103, 108, 179],
}


RATIONALES = {
    "operational_attribution_statement": "Bounded context explicitly attributes generated, co-authored, assisted, reviewed, or synthesized work to the named tool/model.",
    "structured_harness_model_self_report": "Bounded context contains a structured Agent/Model field or execution-log-style model statement.",
    "provider_api_model_dependency_integration": "Bounded context concerns model/provider configuration, support, API behavior, routing, dependency, or application integration.",
    "documentation_benchmark_test_changelog_release": "Bounded context is documentation, setup/release material, a supported-model list, benchmark, or test result.",
    "ordinary_discussion_or_unrelated": "The lexical match is ordinary discussion, a person/place/name collision, or generic language rather than model/harness evidence.",
    "ambiguous": "The bounded context is insufficient to distinguish operational use from naming, documentation, or an unrelated product.",
    "not_reviewable": "The bounded context is corrupted, redacted around the target, or otherwise not interpretable enough for discovery annotation.",
}


def annotation_map(total: int) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for label, ranks in GROUPS.items():
        for rank in ranks:
            if rank in mapping:
                raise RuntimeError(f"Duplicate annotation rank: {rank}")
            mapping[rank] = label
    integration = set(range(1, total + 1)) - set(mapping)
    for rank in integration:
        mapping[rank] = "provider_api_model_dependency_integration"
    if set(mapping) != set(range(1, total + 1)):
        raise RuntimeError("Annotation ranks are not complete")
    return mapping


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def verify_frozen_sample(path: Path) -> str:
    actual = sha256_file(path)
    if actual != EXPECTED_SAMPLE_SHA256:
        raise RuntimeError(
            "Discovery annotations are valid only for the frozen sample "
            f"{EXPECTED_SAMPLE_SHA256}; received {actual}"
        )
    return actual


def annotate(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if len(rows) > 240:
        raise RuntimeError("Authorized 240-row absolute cap exceeded")
    mapping = annotation_map(len(rows))
    output = []
    for row in rows:
        rank = int(row["selection_rank"])
        item = dict(row)
        label = mapping[rank]
        item["discovery_annotation"] = label
        item["annotation_rationale"] = RATIONALES[label]
        output.append(item)
    return output


def summary_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    groups = Counter((row["family_id"], row["discovery_annotation"]) for row in rows)
    family_totals = Counter(row["family_id"] for row in rows)
    output = []
    for (family, label), count in sorted(groups.items()):
        output.append({
            "family_id": family,
            "discovery_annotation": label,
            "sampled_rows": count,
            "family_sample_rows": family_totals[family],
            "sampled_context_proportion": count / family_totals[family],
            "interpretation": "Unweighted proportion in a deterministic stratified discovery sample; not a population proportion.",
        })
    return output


def run(output: Path) -> dict[str, Any]:
    source = output / "DOMESTIC_MODEL_DISCOVERY_SAMPLE.csv"
    sample_sha256 = verify_frozen_sample(source)
    rows = annotate(read_rows(source))
    annotated = output / "DOMESTIC_MODEL_DISCOVERY_ANNOTATED_SAMPLE.csv"
    summary = output / "DOMESTIC_MODEL_DISCOVERY_ANNOTATION_SUMMARY.csv"
    write_csv(annotated, rows)
    write_csv(summary, summary_rows(rows))
    payload = {
        "schema_version": "domestic-model-discovery-annotation-v1",
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "complete_agent_discovery_annotation",
        "annotated_rows": len(rows),
        "absolute_authorized_cap": 240,
        "uncovered_eligible_rows": 6651 - len(rows),
        "method": "single-agent row-by-row reading of deidentified bounded contexts selected by deterministic stratified sampling",
        "terminology": "Agent-coded discovery annotation and sampled context proportions only.",
        "annotation_script_sha256": sha256_file(Path(__file__).resolve()),
        "sample_sha256": sample_sha256,
        "annotated_sample_sha256": sha256_file(annotated),
        "summary_sha256": sha256_file(summary),
    }
    json_dump(output / "DOMESTIC_MODEL_DISCOVERY_ANNOTATION_MANIFEST.json", payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.output.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

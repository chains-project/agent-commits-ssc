"""Materialize and verify 49 offline RQ2 v2 Stage 1-3 scenarios.

[IN]: v2 scenarios/migration JSON and the local synthetic evidence renderer.
[OUT]: Compact stage4_summary.json.
[POS]: Offline Stage 4 validation entry point; canonical-shaped tables exist
only in a temporary directory and no network is accessed.
[SYNC]: Keep the Stage 4 plan, fixture materializer, tests, and RQ2 core design aligned.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "rq2_synthetic_pipeline_v2"
DEFAULT_OUTPUT = ROOT / "results" / "rq2_stage4_synthetic_chain_20260803"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rq2_stage1_extract as stage1  # noqa: E402
import rq2_stage2_build as stage2  # noqa: E402
from rq2_stage2_context import MemoryContextLookup  # noqa: E402
import rq2_stage3_apply as stage3  # noqa: E402
from rq2_stage4_fixture_materializer import (  # noqa: E402
    materialize_stage1_inputs, materialize_stage2_context,
    write_stage3_evidence,
)


KNOWN_GAPS = {
    "npm_first_party": "missing workspace/tree first-party context provider",
    "manifest_lock_resolved": "missing manifest-plus-lock resolved-version context provider",
    "lockfile_only": "Stage 1 excludes lockfile-only events and has no lock context provider",
    "unchanged_missing_dependency": "missing unchanged-manifest context provider",
    "python_alias_mapping_failed": "the import-root policy conservatively emits name-only rather than alias lookup failure",
    "java_maven_property_resolved": "missing Maven property resolved-version context provider",
    "java_gradle_catalog_resolved": "missing Gradle catalog resolved-version context provider",
    "java_mapping_multiple_candidates": "missing Java tree multi-candidate context provider",
}
OUT_OF_SCOPE_LIMITATIONS: set[str] = set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-file", type=Path, default=FIXTURE_DIR / "scenarios.json")
    parser.add_argument("--migration-file", type=Path, default=FIXTURE_DIR / "migration.json")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allow-unexpected", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def run_validation(
    scenario_file: Path, migration_file: Path, output_dir: Path
) -> dict[str, object]:
    spec, migration = read_json(scenario_file), read_json(migration_file)
    _prepare_output(output_dir)
    scenarios = [_resolve_capture_points(item, spec) for item in spec["scenarios"]]
    with tempfile.TemporaryDirectory(prefix="rq2-stage4-") as temporary:
        root = Path(temporary)
        results = [_run_scenario(item, root / str(item["id"])) for item in scenarios]
    summary = _build_summary(spec, migration, results)
    _write_outputs(output_dir, summary)
    return summary


def _resolve_capture_points(scenario, spec) -> dict[str, object]:
    definitions = spec.get("capture_point_definitions", {})
    references = scenario.get("capture_points", spec.get("default_capture_points", []))
    points = [definitions[key] for key in references]
    return {**scenario, "capture_points": points}


def _prepare_output(output_dir: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite Stage 4 output: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)


def _run_scenario(scenario: Mapping[str, object], root: Path) -> dict[str, object]:
    fixture = root / "fixture"
    output = root / "pipeline"
    index, population = materialize_stage1_inputs(scenario, fixture)
    config = stage1.Stage1Config(
        commit_index=index, population_csv=population, output_dir=output,
        actual_languages=(str(scenario["language"]),),
        scan_all_repo_languages=True, sample_per_repo_language=0,
    )
    stage1.run_stage1(config)
    context = MemoryContextLookup(materialize_stage2_context(scenario))
    stage2.run_stage2(output, context_lookup=context)
    episodes = read_csv(output / "episodes.csv")
    evidence = root / "stage3_evidence.jsonl"
    write_stage3_evidence(scenario, episodes, evidence)
    stage3.run_stage3(output, evidence)
    return _evaluate_scenario(scenario, output)


def _evaluate_scenario(scenario: Mapping[str, object], output: Path) -> dict[str, object]:
    commits = read_csv(output / "commits.csv")
    events = read_csv(output / "events.csv")
    episodes = read_csv(output / "episodes.csv")
    links = read_csv(output / "episode_event_links.csv")
    expected = _expected_summary(scenario["expected"])
    actual = _actual_summary(commits, events, episodes, links)
    checks = _stage1_checks(expected["stage1"], actual["stage1"])
    checks.extend(_episode_checks(expected["episodes"], actual["episodes"]))
    mismatches = [row for row in checks if not row["passed"]]
    status = _scenario_status(str(scenario["id"]), mismatches)
    result = {
        "scenario_id": scenario["id"], "language": scenario["language"],
        "execution_mode": scenario["execution_mode"],
        "input_summary": scenario["input_summary"],
        "capture_points": scenario["capture_points"],
        "expected": expected, "actual": actual, "status": status,
    }
    if mismatches:
        result.update(
            known_gap=KNOWN_GAPS.get(str(scenario["id"]), ""),
            checks=len(checks), failed_checks=len(mismatches), mismatches=mismatches,
        )
    return result


def _actual_summary(commits, events, episodes, links) -> dict[str, object]:
    stage1 = {
        "candidate": bool(commits),
        "import_events": sum(row["event_type"] == "import" for row in events),
        "manifest_events": sum(row["event_type"] == "manifest_addition" for row in events),
        "import_ordinals": sorted(
            int(row["event_ordinal"])
            for row in events if row["event_type"] == "import"
        ),
    }
    stage1["detected_languages"] = sorted({
        value for row in commits
        for value in row["actual_changed_languages"].split("|") if value
    })
    episode_rows = sorted(
        (_actual_episode(row, events, links) for row in episodes), key=_sort_key
    )
    return {"stage1": stage1, "episodes": episode_rows}


def _expected_summary(expected) -> dict[str, object]:
    stage1 = dict(expected["stage1"])
    episodes = sorted((_expected_episode(row) for row in expected["episodes"]), key=_sort_key)
    return {"stage1": stage1, "episodes": episodes}


def _stage1_checks(expected, actual) -> list[dict[str, object]]:
    return [_check(f"stage1.{key}", value, actual[key]) for key, value in expected.items()]


def _episode_checks(expected_rows, actual_rows) -> list[dict[str, object]]:
    return [
        _check("episodes.count", len(expected_rows), len(actual_rows)),
        _check("episodes.semantic_rows", expected_rows, actual_rows),
    ]


def _expected_episode(row: Mapping[str, object]) -> dict[str, object]:
    query = row["query"]
    result = {
        "quadrant": row["quadrant"], "alignment_status": row["alignment_status"],
        "ecosystem": query["ecosystem"], "package": query["package"],
        "query_kind": query["kind"], "query_value": query["value"],
        "registry_status": row["registry_status"], "advisory_status": row["advisory_status"],
        "reasons": sorted(row["reasons"]),
    }
    for key in ("linked_import_events", "registry_query_count"):
        if key in row:
            result[key] = row[key]
    return result


def _actual_episode(row, events, links) -> dict[str, object]:
    event_by_id = {event["event_id"]: event for event in events}
    linked = [link for link in links if link["episode_id"] == row["episode_id"]]
    imports = sum(event_by_id[item["event_id"]]["event_type"] == "import" for item in linked)
    result = {
        "quadrant": row["dependency_quadrant"], "alignment_status": row["alignment_status"],
        "ecosystem": row["ecosystem"], "package": row["package_name"],
        "query_kind": row["query_kind"], "query_value": row["query_value"],
        "registry_status": row["registry_status"], "advisory_status": row["advisory_status"],
        "reasons": sorted(value for value in row["reason_codes"].split("|") if value),
    }
    if imports > 1:
        result["linked_import_events"] = imports
        result["registry_query_count"] = 1 if row["query_kind"] in {"exact", "name_only", "range"} else 0
    return result


def _sort_key(row: Mapping[str, object]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def _check(field: str, expected: object, actual: object) -> dict[str, object]:
    return {"field": field, "expected": expected, "actual": actual, "passed": expected == actual}


def _scenario_status(scenario_id: str, mismatches: Sequence[object]) -> str:
    if not mismatches:
        return "pass"
    if scenario_id in OUT_OF_SCOPE_LIMITATIONS:
        return "out_of_scope_limitation"
    return "known_implementation_gap" if scenario_id in KNOWN_GAPS else "unexpected_regression"


def _build_summary(spec, migration, results) -> dict[str, object]:
    statuses = Counter(row["status"] for row in results)
    retained = {item["id"]: item["migration"] for item in migration["items"]}
    v1_rows = [
        {"scenario_id": row["scenario_id"], "migration": retained[row["scenario_id"]], "status": row["status"]}
        for row in results if row["scenario_id"] in retained
    ]
    in_scope = [row for row in results if row["status"] != "out_of_scope_limitation"]
    return {
        "schema_version": 2, "mode": "offline_synthetic_fixed_evidence",
        "network_access": False, "scenario_count": len(results),
        "status_counts": dict(sorted(statuses.items())), "results": results,
        "v1_retained_count": len(v1_rows), "v1_regression": v1_rows,
        "target_languages": spec["target_languages"],
        "coverage_matrix": spec.get("coverage_matrix", {}),
        "in_scope_count": len(in_scope),
        "in_scope_pass_count": sum(row["status"] == "pass" for row in in_scope),
        "canonical_csv_persisted": 0,
    }


def _write_outputs(output_dir: Path, summary: Mapping[str, object]) -> None:
    summary_path = output_dir / "stage4_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    summary = run_validation(args.scenario_file, args.migration_file, args.output_dir)
    print(json.dumps(summary["status_counts"], ensure_ascii=False, sort_keys=True))
    if summary["status_counts"].get("unexpected_regression") and not args.allow_unexpected:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

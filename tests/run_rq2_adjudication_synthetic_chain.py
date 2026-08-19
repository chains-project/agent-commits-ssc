"""Run the offline RQ2 Stage 1-to-final-adjudication synthetic chain.

[IN]: Nine independent adjudication scenarios with fixed registry and sidecar evidence.
[OUT]: summary.json containing Stage 3 and final-label checks.
[POS]: Additive regression runner; canonical six-language products remain read-only.
[SYNC]: Keep the adjudication fixture, materializer, tests, and plan aligned.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "tests"
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
FIXTURE_DIR = TEST_DIR / "fixtures" / "rq2_adjudication_synthetic_v1"
PUBLIC_DIR = ROOT / "scripts" / "rq2" / "adjudication"
ADJUDICATION_SCRIPT = PUBLIC_DIR / "build_final_adjudication.py"
COMMENT_GUARD = PUBLIC_DIR / "apply_commented_manifest_corrections.py"
DEFAULT_OUTPUT = (ROOT / "results" /
                  "rq2_adjudication_synthetic_chain_20260819_consolidated")
ScriptPair = tuple[Path, Path]
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(TEST_DIR))

import rq2_stage1_extract as stage1  # noqa: E402
import rq2_stage2_build as stage2  # noqa: E402
from rq2_stage2_context import MemoryContextLookup  # noqa: E402
import rq2_stage3_apply as stage3  # noqa: E402
from rq2_stage4_fixture_materializer import (  # noqa: E402
    materialize_stage1_inputs,
    materialize_stage2_context,
)
from rq2_adjudication_fixture_materializer import (  # noqa: E402
    materialize_adjudication_inputs,
    write_stage3_evidence,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario-file", type=Path, default=FIXTURE_DIR / "scenarios.json"
    )
    parser.add_argument(
        "--adjudication-script", type=Path, default=ADJUDICATION_SCRIPT
    )
    parser.add_argument("--comment-guard", type=Path, default=COMMENT_GUARD)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def run_validation(
    scenario_file: Path,
    output_dir: Path,
    adjudication_script: Path = ADJUDICATION_SCRIPT,
    comment_guard: Path = COMMENT_GUARD,
) -> dict[str, object]:
    spec = read_json(scenario_file)
    _prepare_output(output_dir)
    with tempfile.TemporaryDirectory(prefix="rq2-adjudication-") as temporary:
        root = Path(temporary)
        results = [
            _run_scenario(
                item, root / str(item["id"]), (adjudication_script, comment_guard)
            )
            for item in spec["scenarios"]
        ]
    summary = _build_summary(spec, results)
    _write_outputs(output_dir, summary)
    return summary


def _prepare_output(output_dir: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(
            f"refusing to overwrite adjudication synthetic output: {output_dir}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)


def _run_scenario(
    scenario: Mapping[str, object],
    root: Path,
    scripts: ScriptPair,
) -> dict[str, object]:
    pipeline, patch = _run_stage1_to_stage3(scenario, root)
    episodes = read_csv(pipeline / "episodes.csv")
    events = read_csv(pipeline / "events.csv")
    links = read_csv(pipeline / "episode_event_links.csv")
    episode = _single_episode(scenario, episodes)
    sidecars = materialize_adjudication_inputs(
        scenario, episode, events, links, patch, root / "adjudication_inputs"
    )
    outputs = _run_adjudication(sidecars, root / "adjudication", scripts)
    return _evaluate(scenario, episode, outputs, scripts)


def _run_stage1_to_stage3(
    scenario: Mapping[str, object],
    root: Path,
) -> tuple[Path, Path]:
    fixture = root / "fixture"
    pipeline = root / "pipeline"
    index, population = materialize_stage1_inputs(scenario, fixture)
    config = stage1.Stage1Config(
        commit_index=index,
        population_csv=population,
        output_dir=pipeline,
        actual_languages=(str(scenario["language"]),),
        scan_all_repo_languages=True,
        sample_per_repo_language=0,
    )
    stage1.run_stage1(config)
    context = MemoryContextLookup(materialize_stage2_context(scenario))
    stage2.run_stage2(pipeline, context_lookup=context)
    evidence = root / "stage3_evidence.jsonl"
    write_stage3_evidence(scenario, read_csv(pipeline / "episodes.csv"), evidence)
    stage3.run_stage3(pipeline, evidence)
    return pipeline, fixture / "commit.diff"


def _single_episode(
    scenario: Mapping[str, object],
    episodes: Sequence[Mapping[str, str]],
) -> Mapping[str, str]:
    if len(episodes) != 1:
        raise ValueError(
            f"{scenario['id']} expected one dependency episode, got {len(episodes)}"
        )
    return episodes[0]


def _run_adjudication(
    sidecars: Mapping[str, Path],
    root: Path,
    scripts: ScriptPair,
) -> dict[str, Path]:
    adjudication_script, comment_guard = scripts
    root.mkdir(parents=True)
    raw = root / "adjudication_raw.csv"
    final = root / "adjudication.csv"
    audit = root / "comment_corrections.csv"
    guard_summary = root / "comment_guard_summary.json"
    command = [sys.executable, str(adjudication_script)]
    for name, path in sidecars.items():
        command.extend([f"--{name.replace('_', '-')}", str(path)])
    command.extend(["--output", str(raw)])
    _run_cli(command, adjudication_script.parent)
    _run_comment_guard(
        comment_guard, raw, sidecars["evidence_csv"], final, audit,
        guard_summary,
    )
    return {
        "raw": raw,
        "final": final,
        "audit": audit,
        "guard_summary": guard_summary,
    }


def _run_comment_guard(
    script: Path,
    raw: Path,
    evidence: Path,
    final: Path,
    audit: Path,
    summary: Path,
) -> None:
    command = [
        sys.executable,
        str(script),
        "--adjudication", str(raw),
        "--evidence", str(evidence),
        "--output", str(final),
        "--audit", str(audit),
        "--summary", str(summary),
    ]
    _run_cli(command, script.parent)


def _run_cli(command: Sequence[str], cwd: Path) -> None:
    subprocess.run(
        list(command),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _evaluate(
    scenario: Mapping[str, object],
    episode: Mapping[str, str],
    outputs: Mapping[str, Path],
    scripts: ScriptPair,
) -> dict[str, object]:
    raw = _single_row(outputs["raw"])
    final = _single_row(outputs["final"])
    guard = read_json(outputs["guard_summary"])
    expected = scenario["expected"]
    actual = _actual(episode, raw, final, guard)
    checks = _checks(expected, actual)
    return {
        "scenario_id": scenario["id"],
        "language": scenario["language"],
        "input_summary": scenario["input_summary"],
        "package": episode["package_name"],
        "query_kind": episode["query_kind"],
        "query_value": episode["query_value"],
        "capture_points": _capture_points(scripts),
        "expected": expected,
        "actual": actual,
        "checks": checks,
        "status": "pass" if all(row["passed"] for row in checks) else "fail",
    }




def _capture_points(scripts: ScriptPair) -> list[str]:
    return [
        "rq2_stage1_extract.run_stage1", "rq2_stage2_build.run_stage2",
        "rq2_stage3_apply.run_stage3", scripts[0].name, scripts[1].name,
    ]


def _single_row(path: Path) -> dict[str, str]:
    rows = read_csv(path)
    if len(rows) != 1:
        raise ValueError(f"expected one adjudication row in {path}, got {len(rows)}")
    return rows[0]


def _actual(
    episode: Mapping[str, str],
    raw: Mapping[str, str],
    final: Mapping[str, str],
    guard: Mapping[str, object],
) -> dict[str, object]:
    return {
        "stage3_registry_status": episode["registry_status"],
        "raw_final_label": raw["final_label"],
        "final_label": final["final_label"],
        "confidence": final["confidence"],
        "adjudication_basis": final["adjudication_basis"],
        "evidence_flags": json.loads(final["evidence_flags_json"]),
        "commented_manifest_corrections": guard[
            "commented_manifest_corrections"
        ],
    }


def _checks(
    expected: Mapping[str, object],
    actual: Mapping[str, object],
) -> list[dict[str, object]]:
    checks = [
        _equal("stage3_registry_status", expected, actual),
        _equal("final_label", expected, actual),
        _equal("confidence", expected, actual),
        _equal("commented_manifest_corrections", expected, actual),
        _contains("basis_contains", expected, actual["adjudication_basis"]),
        _subset("flags_include", expected, actual["evidence_flags"]),
    ]
    checks.append({
        "field": "comment_guard_preserves_expected_label",
        "expected": expected["final_label"],
        "actual": actual["raw_final_label"],
        "passed": actual["raw_final_label"] == expected["final_label"],
    })
    return checks


def _equal(
    field: str,
    expected: Mapping[str, object],
    actual: Mapping[str, object],
) -> dict[str, object]:
    return {
        "field": field,
        "expected": expected[field],
        "actual": actual[field],
        "passed": expected[field] == actual[field],
    }


def _contains(
    field: str,
    expected: Mapping[str, object],
    actual: object,
) -> dict[str, object]:
    fragment = str(expected[field])
    return {
        "field": field,
        "expected": fragment,
        "actual": actual,
        "passed": fragment in str(actual),
    }


def _subset(
    field: str,
    expected: Mapping[str, object],
    actual: object,
) -> dict[str, object]:
    required = set(expected[field])
    observed = set(actual)
    return {
        "field": field,
        "expected": sorted(required),
        "actual": sorted(observed),
        "passed": required <= observed,
    }


def _build_summary(
    spec: Mapping[str, object],
    results: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    statuses = Counter(row["status"] for row in results)
    labels = Counter(row["actual"]["final_label"] for row in results)
    languages = sorted({str(row["language"]) for row in results})
    return {
        "schema_version": 1,
        "mode": "offline_synthetic_fixed_evidence",
        "network_access": False,
        "scenario_count": len(results),
        "status_counts": dict(sorted(statuses.items())),
        "label_counts": dict(sorted(labels.items())),
        "languages": languages,
        "all_target_languages_covered": set(spec["target_languages"]) == set(languages),
        "all_target_labels_covered": set(spec["target_labels"]) == set(labels),
        "canonical_csv_persisted": 0,
        "results": list(results),
    }


def _write_outputs(
    output_dir: Path,
    summary: Mapping[str, object],
) -> None:
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    summary = run_validation(
        args.scenario_file, args.output_dir,
        args.adjudication_script, args.comment_guard,
    )
    print(json.dumps(summary["status_counts"], sort_keys=True))
    if summary["status_counts"].get("fail"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

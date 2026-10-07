"""Run Phase9 improvements in the selected changed-language order.

[IN]: Local commit/patch index, agent population, previous context/registry/OSV outputs and cache.

[OUT]: Six language directories with Stage1-3 tables, sidecars, checkpoints, status JSON and reports.

[POS]: Resumable orchestration writing new output directories; preserves previous products.

[SYNC]: Keep phase boundaries, language order, output documentation and tests aligned.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import stage1_extract as stage1
import stage2_build as stage2
import stage3_apply as stage3
from stage2_context import ContextSource, build_legacy_context_database
from stage3_collect import (
    DEFAULT_DATA_ROOT, DEFAULT_LEGACY_ROOT, _legacy_directories, run_collector,
)
from streaming_store import atomic_write_json


LANGUAGES = ("TypeScript", "Python", "JavaScript", "Rust", "Go", "Java")
DEFAULT_OUTPUT = DEFAULT_DATA_ROOT / "data_products" / "rq2_phase9_supervisor_refined_20260804"
DEFAULT_LEGACY_CACHE = DEFAULT_DATA_ROOT / "cache" / "rq2_hallucinated" / "registry"
DEFAULT_V2_CACHE = DEFAULT_DATA_ROOT / "cache" / "rq2_stage3_v2"
DEFAULT_PYTHON_WHEEL_MAP = (
    DEFAULT_DATA_ROOT / "cache" / "rq2_hallucinated" / "python_wheel_import_map.csv"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--commit-index", type=Path, default=stage1.DEFAULT_INDEX)
    parser.add_argument("--population-csv", type=Path, default=stage1.DEFAULT_POPULATION)
    parser.add_argument("--legacy-root", type=Path, default=DEFAULT_LEGACY_ROOT)
    parser.add_argument("--legacy-cache-root", type=Path, default=DEFAULT_LEGACY_CACHE)
    parser.add_argument("--v2-cache-root", type=Path, default=DEFAULT_V2_CACHE)
    parser.add_argument(
        "--python-wheel-map", type=Path, default=DEFAULT_PYTHON_WHEEL_MAP
    )
    parser.add_argument("--languages", nargs="*", default=list(LANGUAGES))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-index-rows", type=int, default=0)
    parser.add_argument("--max-total-candidates", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--maven-workers", type=int, default=2)
    return parser.parse_args()


def run_experiment(args: argparse.Namespace) -> dict[str, object]:
    root = args.output_root
    root.mkdir(parents=True, exist_ok=True)
    state = _read_state(root)
    state.update({
        "started_utc": state.get("started_utc", _now()),
        "requested_languages": list(args.languages), "complete": False,
    })
    for language in args.languages:
        if language not in LANGUAGES:
            raise ValueError(f"unsupported experiment language: {language}")
        _run_language(args, language, state)
        _write_state(root, state)
    state["complete"] = all(
        state["languages"].get(language, {}).get("complete") for language in args.languages
    )
    state["completed_utc"] = _now()
    _write_state(root, state)
    return state


def _run_language(args, language: str, state: dict[str, object]) -> None:
    language_dir = args.output_root / language
    context_db = args.output_root / "context" / f"{language}.sqlite"
    entry = state.setdefault("languages", {}).setdefault(language, {})
    entry["started_utc"] = entry.get("started_utc", _now())
    entry["context"] = build_legacy_context_database(
        context_db, _context_sources(
            args.legacy_root, language, args.python_wheel_map
        )
    )
    _write_state(args.output_root, state)
    entry["stage1"] = stage1.run_stage1(_stage1_config(args, language, language_dir))
    _write_state(args.output_root, state)
    entry["stage2"] = stage2.run_stage2(language_dir, resume=True, context_db=context_db)
    _write_state(args.output_root, state)
    entry["collector"] = _collect(args, language_dir)
    _write_state(args.output_root, state)
    if not entry["collector"]["complete"]:
        raise RuntimeError(f"collector incomplete for {language}")
    evidence = language_dir / "stage3_evidence_input.jsonl"
    entry["stage3"] = stage3.run_stage3(language_dir, evidence, resume=True)
    entry["complete"] = True
    entry["completed_utc"] = _now()


def _stage1_config(args, language: str, output: Path) -> stage1.Stage1Config:
    return stage1.Stage1Config(
        commit_index=args.commit_index, population_csv=args.population_csv,
        output_dir=output, actual_languages=(language,),
        scan_all_repo_languages=True, sample_per_repo_language=0,
        max_index_rows=max(0, args.max_index_rows),
        max_total_candidates=max(0, args.max_total_candidates),
        resume=True, checkpoint_every=1000, max_buffer_rows=100_000,
    )


def _context_sources(
    root: Path, language: str, wheel_map: Path
) -> list[ContextSource]:
    directory = root / language / "manifest_registry"
    sources = [ContextSource(language, directory / "import_dependency_matches.csv")]
    if language == "Python":
        sources.extend((
            ContextSource(
                language, directory / "parsed_dependencies.csv",
                "python_dependencies",
            ),
            ContextSource(language, wheel_map, "python_wheel_map"),
        ))
    if language in {"Go", "Java"}:
        sources.append(ContextSource(language, directory / "parsed_dependencies.csv", "parsed_dependencies"))
    return sources


def _collect(args, language_dir: Path) -> dict[str, object]:
    return run_collector(
        language_dir, language_dir / "stage3_evidence_input.jsonl",
        language_dir / "stage3_collector.sqlite", args.v2_cache_root,
        args.legacy_cache_root, _legacy_directories(args.legacy_root),
        resume=True, timeout=args.timeout,
        registry_batch_size=1 if language_dir.name == "Go" else 50,
        maven_workers=args.maven_workers if language_dir.name == "Java" else 1,
    )


def _read_state(root: Path) -> dict[str, object]:
    path = root / "experiment_state.json"
    if not path.is_file():
        return {"schema_version": 1, "language_order": list(LANGUAGES), "languages": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("experiment state must be an object")
    return value


def _write_state(root: Path, state: Mapping[str, object]) -> None:
    state = {**state, "updated_utc": _now()}
    atomic_write_json(root / "experiment_state.json", state)
    _write_report(root / "experiment_status_zh.md", state)


def _write_report(path: Path, state: Mapping[str, object]) -> None:
    rows = ["# Phase9 experiment status", ""]
    rows.append(f"- Requested run complete: {state.get('complete', False)}")
    rows.append(f"- Updated at: {state.get('updated_utc', '')}")
    rows.extend(("", "## Language progress", ""))
    languages = state.get("languages", {})
    for language in LANGUAGES:
        item = languages.get(language, {}) if isinstance(languages, Mapping) else {}
        rows.append(f"- {language}: {'complete' if item.get('complete') else 'incomplete or running'}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> None:
    summary = run_experiment(parse_args())
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

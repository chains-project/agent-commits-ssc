"""Materialize deterministic inputs for the RQ2 final-adjudication synthetic chain.

[IN]: One adjudication scenario plus its real Stage 1-3 episode/events/links.
[OUT]: Fixed Stage 3 evidence and the 14 CSV sidecars consumed by v6 adjudication.
[POS]: Test-fixture adapter only; no network or canonical experiment product is touched.
[SYNC]: Keep scenarios.json, run_rq2_adjudication_synthetic_chain.py, and tests aligned.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Mapping, Sequence

from rq2_stage4_fixture_materializer import scenario_repo, scenario_sha


QUERY_TIME = "2026-08-19T12:00:00Z"
REGISTRY_SOURCES = {
    "npm": "npm",
    "PyPI": "PyPI",
    "Cargo": "crates.io",
    "Go": "Go proxy",
    "Maven": "Maven Central",
}
INPUT_FIELDS = {
    "candidate_csv": [
        "case_id", "episode_id", "languages_json", "repo", "sha", "agent",
        "ecosystem", "package_name", "query_kind", "query_value", "registry_status",
    ],
    "evidence_csv": ["case_id", "episode_id", "events_json", "patch_path"],
    "context_csv": ["case_id", "episode_id", "context_hints_json"],
    "python_mapping": ["episode_id"],
    "python_alternate": ["episode_id"],
    "temporal_queue": [
        "case_id", "episode_id", "parent_state", "exclusion_flags_json",
        "strong_review_candidate",
    ],
    "temporal_requery": [
        "case_id", "episode_id", "effective_package_name", "fresh_decision",
        "fresh_publish_after_committer_seconds",
    ],
    "temporal_npm_semver": ["episode_id"],
    "current_requery": [
        "case_id", "episode_id", "effective_package_name",
        "patch_nonpublic_spec_hint", "fresh_current_decision",
    ],
    "current_npm_semver": ["episode_id"],
    "current_temporal": ["episode_id"],
    "maven_requery": ["episode_id"],
    "maven_times": ["episode_id"],
    "hidden_postdate": ["episode_id"],
}


def write_stage3_evidence(
    scenario: Mapping[str, object],
    episodes: Sequence[Mapping[str, str]],
    path: Path,
) -> None:
    records: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for episode in episodes:
        if episode["query_kind"] not in {"exact", "name_only", "range"}:
            continue
        key = (episode["ecosystem"], episode["package_name"])
        if key not in seen:
            records.append(_registry_record(scenario, episode))
            seen.add(key)
        records.append(_advisory_record(episode))
    payload = "\n".join(json.dumps(row, sort_keys=True) for row in records)
    path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")


def _registry_record(
    scenario: Mapping[str, object],
    episode: Mapping[str, str],
) -> dict[str, object]:
    registry = scenario["registry"]
    return {
        "evidence_type": "registry",
        "source": REGISTRY_SOURCES[episode["ecosystem"]],
        "ecosystem": episode["ecosystem"],
        "package_name": episode["package_name"],
        "lookup_status": registry["lookup_status"],
        "queried_at": QUERY_TIME,
        "versions": registry["versions"],
    }


def _advisory_record(episode: Mapping[str, str]) -> dict[str, object]:
    return {
        "evidence_type": "advisory",
        "source": "OSV",
        "ecosystem": episode["ecosystem"],
        "package_name": episode["package_name"],
        "query_kind": episode["query_kind"],
        "query_value": episode["query_value"],
        "lookup_status": "ok",
        "queried_at": QUERY_TIME,
        "advisories": [],
    }


def materialize_adjudication_inputs(
    scenario: Mapping[str, object],
    episode: Mapping[str, str],
    events: Sequence[Mapping[str, str]],
    links: Sequence[Mapping[str, str]],
    patch_path: Path,
    root: Path,
) -> dict[str, Path]:
    root.mkdir(parents=True)
    rows = _input_rows(scenario, episode, events, links, patch_path)
    paths: dict[str, Path] = {}
    for name, fields in INPUT_FIELDS.items():
        path = root / f"{name}.csv"
        _write_csv(path, fields, rows.get(name, []))
        paths[name] = path
    return paths


def _input_rows(
    scenario: Mapping[str, object],
    episode: Mapping[str, str],
    events: Sequence[Mapping[str, str]],
    links: Sequence[Mapping[str, str]],
    patch_path: Path,
) -> dict[str, list[dict[str, str]]]:
    rows = {
        "candidate_csv": [_candidate_row(scenario, episode)],
        "evidence_csv": [
            _evidence_row(scenario, episode, events, links, patch_path)
        ],
        "context_csv": [_context_row(scenario, episode)],
    }
    rows.update(_optional_rows(scenario, episode))
    return rows


def _identity(
    scenario: Mapping[str, object],
    episode: Mapping[str, str],
) -> dict[str, str]:
    return {
        "case_id": f"synthetic:{scenario['id']}",
        "episode_id": episode["episode_id"],
    }


def _candidate_row(
    scenario: Mapping[str, object],
    episode: Mapping[str, str],
) -> dict[str, str]:
    return {
        **_identity(scenario, episode),
        "languages_json": json.dumps([scenario["language"]]),
        "repo": scenario_repo(scenario),
        "sha": scenario_sha(scenario),
        "agent": "synthetic_adjudication",
        "ecosystem": episode["ecosystem"],
        "package_name": episode["package_name"],
        "query_kind": episode["query_kind"],
        "query_value": episode["query_value"],
        "registry_status": episode["registry_status"],
    }


def _evidence_row(
    scenario: Mapping[str, object],
    episode: Mapping[str, str],
    events: Sequence[Mapping[str, str]],
    links: Sequence[Mapping[str, str]],
    patch_path: Path,
) -> dict[str, str]:
    linked = _linked_events(episode["episode_id"], events, links)
    return {
        **_identity(scenario, episode),
        "events_json": json.dumps(linked, sort_keys=True),
        "patch_path": str(patch_path.resolve()),
    }


def _linked_events(
    episode_id: str,
    events: Sequence[Mapping[str, str]],
    links: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    linked_ids = {
        row["event_id"] for row in links if row["episode_id"] == episode_id
    }
    return [
        {
            "event_type": row["event_type"],
            "path": row["path"],
            "new_line_number": row["new_line_number"],
        }
        for row in events if row["event_id"] in linked_ids
    ]


def _context_row(
    scenario: Mapping[str, object],
    episode: Mapping[str, str],
) -> dict[str, str]:
    inputs = scenario.get("adjudication_inputs", {})
    hints = inputs.get("context_hints", [])
    return {
        **_identity(scenario, episode),
        "context_hints_json": json.dumps(hints, sort_keys=True),
    }


def _optional_rows(
    scenario: Mapping[str, object],
    episode: Mapping[str, str],
) -> dict[str, list[dict[str, str]]]:
    inputs = scenario.get("adjudication_inputs", {})
    rows: dict[str, list[dict[str, str]]] = {}
    if "temporal_queue" in inputs:
        rows["temporal_queue"] = [
            _temporal_queue_row(scenario, episode, inputs["temporal_queue"])
        ]
    if "temporal_requery" in inputs:
        rows["temporal_requery"] = [
            _temporal_requery_row(scenario, episode, inputs["temporal_requery"])
        ]
    if "current_requery" in inputs:
        rows["current_requery"] = [
            _current_requery_row(scenario, episode, inputs["current_requery"])
        ]
    return rows


def _temporal_queue_row(
    scenario: Mapping[str, object],
    episode: Mapping[str, str],
    values: Mapping[str, object],
) -> dict[str, str]:
    return {
        **_identity(scenario, episode),
        "parent_state": str(values["parent_state"]),
        "exclusion_flags_json": json.dumps(values["exclusion_flags"]),
        "strong_review_candidate": str(values["strong_review_candidate"]).lower(),
    }


def _temporal_requery_row(
    scenario: Mapping[str, object],
    episode: Mapping[str, str],
    values: Mapping[str, object],
) -> dict[str, str]:
    return {
        **_identity(scenario, episode),
        "effective_package_name": str(values["effective_package_name"]),
        "fresh_decision": str(values["fresh_decision"]),
        "fresh_publish_after_committer_seconds": str(
            values["fresh_publish_after_committer_seconds"]
        ),
    }


def _current_requery_row(
    scenario: Mapping[str, object],
    episode: Mapping[str, str],
    values: Mapping[str, object],
) -> dict[str, str]:
    return {
        **_identity(scenario, episode),
        "effective_package_name": str(values["effective_package_name"]),
        "patch_nonpublic_spec_hint": str(
            values["patch_nonpublic_spec_hint"]
        ).lower(),
        "fresh_current_decision": str(values["fresh_current_decision"]),
    }


def _write_csv(
    path: Path,
    fields: Sequence[str],
    rows: Sequence[Mapping[str, str]],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        writer.writerows(rows)

"""Rebuild missing completion metadata for committed RQ2 Stage 3 data.

[IN]: Enriched canonical tables, fixed evidence, an isolated Stage 2 baseline,
and the official Stage 2 state.
[OUT]: Evidence metadata/cache, Stage 3 summary, report, and completed state.
[POS]: Narrow transactional repair; does not rewrite the canonical CSV tables
or rerun evidence decisions.
[SYNC]: Keep rq2_stage3_streaming.py, tests, scripts/CLAUDE.md, and
scripts/OUTPUTS.md synchronized with repair preconditions or outputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import rq2_stage3_apply as stage3_apply
from rq2_stage3_streaming import (
    FINAL_OUTPUTS,
    STATE_NAME,
    EvidenceOffsetIndex,
    _write_used_evidence,
)
from rq2_stage3_version_query import PACKAGING_VERSION
from rq2_stage_schema import SCHEMA_VERSION, table_contract, validate_row
from rq2_streaming_store import _replace_with_retry, atomic_write_json, read_json


WORK_NAME = ".stage3_metadata_repair_work"


@dataclass(frozen=True)
class EpisodeAudit:
    rows: int
    bytes: int
    sha256: str
    used_ids: tuple[str, ...]
    registry: Mapping[str, int]
    advisory: Mapping[str, int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--evidence-input", type=Path, required=True)
    parser.add_argument("--stage2-dir", type=Path, required=True)
    parser.add_argument("--stage2-state", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def repair_stage3_metadata(
    input_dir: Path, evidence_input: Path, stage2_dir: Path,
    stage2_state_path: Path, *, apply: bool = False,
) -> dict[str, object]:
    input_dir, stage2_dir = Path(input_dir), Path(stage2_dir)
    _require_repair_state(input_dir, Path(evidence_input))
    stage2_state = read_json(Path(stage2_state_path))
    stage2_hash = _validate_stage2_baseline(input_dir, stage2_dir, stage2_state)
    audit = _audit_final_episodes(input_dir, stage2_state)
    evidence = EvidenceOffsetIndex(Path(evidence_input))
    _require_evidence_ids(audit, evidence)
    preview = _preview(input_dir, evidence, stage2_hash, audit)
    if not apply:
        return preview
    return _apply_repair(input_dir, evidence, stage2_hash, audit, preview)


def _require_repair_state(input_dir: Path, evidence_input: Path) -> None:
    required = [input_dir / name for name in _table_names()]
    required.extend((input_dir / "schema_manifest.json", evidence_input))
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing repair inputs: {missing}")
    existing = [name for name in FINAL_OUTPUTS if (input_dir / name).exists()]
    if existing:
        raise FileExistsError(f"Stage 3 metadata already exists: {existing}")
    if (input_dir / WORK_NAME).exists():
        raise FileExistsError(f"repair work directory exists: {WORK_NAME}")


def _validate_stage2_baseline(input_dir, stage2_dir, state) -> str:
    episodes, links = stage2_dir / "episodes.csv", stage2_dir / "episode_event_links.csv"
    if not episodes.is_file() or not links.is_file():
        raise FileNotFoundError("isolated Stage 2 baseline is incomplete")
    offsets = state.get("csv_bytes", {})
    _require_size(episodes, int(offsets.get("episodes", -1)))
    _require_size(links, int(offsets.get("links", -1)))
    _require_rows(episodes, int(state["episodes"]), require_stage2=True)
    _require_rows(links, int(state["episode_event_links"]))
    if _sha256(links) != _sha256(input_dir / "episode_event_links.csv"):
        raise ValueError("isolated Stage 2 links differ from formal links")
    return _sha256(episodes)


def _require_size(path: Path, expected: int) -> None:
    if path.stat().st_size != expected:
        raise ValueError(f"unexpected Stage 2 byte count: {path.name}")


def _require_rows(path: Path, expected: int, require_stage2: bool = False) -> None:
    fields = table_contract(path.name).fields
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != fields:
            raise ValueError(f"invalid header: {path.name}")
        for row in reader:
            count += 1
            if require_stage2 and _queryable(row):
                if row["registry_status"] != "not_queried":
                    raise ValueError("baseline episodes are not Stage 2 rows")
    if count != expected:
        raise ValueError(f"unexpected Stage 2 row count: {path.name}")


def _audit_final_episodes(input_dir: Path, state) -> EpisodeAudit:
    path = input_dir / "episodes.csv"
    manifest = read_json(input_dir / "schema_manifest.json")
    expected = manifest["tables"]["episodes.csv"]
    rows, used, registry, advisory = _scan_episodes(path)
    digest = _sha256(path)
    if rows != int(state["episodes"]) or rows != int(expected["row_count"]):
        raise ValueError("formal Stage 3 episode row count mismatch")
    if path.stat().st_size != int(expected["bytes"]) or digest != expected["sha256"]:
        raise ValueError("formal Stage 3 episode manifest mismatch")
    return EpisodeAudit(
        rows, path.stat().st_size, digest, tuple(sorted(used)),
        dict(sorted(registry.items())), dict(sorted(advisory.items())),
    )


def _scan_episodes(path: Path):
    fields = table_contract("episodes.csv").fields
    rows, used = 0, set()
    registry, advisory = Counter(), Counter()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != fields:
            raise ValueError("invalid formal episodes header")
        for row in reader:
            validate_row("episodes.csv", row)
            _validate_enriched_row(row)
            rows += 1
            registry[row["registry_status"]] += 1
            advisory[row["advisory_status"]] += 1
            used.update(token for token in row["evidence_ids"].split("|") if token)
    return rows, used, registry, advisory


def _validate_enriched_row(row: Mapping[str, str]) -> None:
    if not _queryable(row):
        return
    if row["registry_status"] == "not_queried":
        raise ValueError(f"queryable episode lacks registry result: {row['episode_id']}")
    evidence_ids = [token for token in row["evidence_ids"].split("|") if token]
    if len(evidence_ids) < 2:
        raise ValueError(f"queryable episode lacks evidence IDs: {row['episode_id']}")


def _queryable(row: Mapping[str, str]) -> bool:
    return row["query_kind"] not in {"excluded", "unresolved"}


def _require_evidence_ids(audit: EpisodeAudit, evidence: EvidenceOffsetIndex) -> None:
    missing = set(audit.used_ids) - set(evidence.id_locators)
    if missing:
        raise ValueError(f"missing used evidence IDs: {len(missing)}")


def _preview(input_dir, evidence, stage2_hash, audit) -> dict[str, object]:
    return {
        "mode": "stage3_metadata_repair_preview",
        "episodes": audit.rows,
        "stage2_episode_sha256": stage2_hash,
        "stage3_episode_sha256": audit.sha256,
        "evidence_input_sha256": evidence.sha256,
        "used_evidence_ids": len(audit.used_ids),
        "registry_statuses": dict(audit.registry),
        "advisory_statuses": dict(audit.advisory),
        "protected_table_sha256": _protected_hashes(input_dir),
    }


def _apply_repair(input_dir, evidence, stage2_hash, audit, preview):
    work = input_dir / WORK_NAME
    work.mkdir()
    committed = False
    try:
        records, payloads = _write_used_evidence(work, evidence, set(audit.used_ids))
        summary = _summary(input_dir, audit, preview, records, payloads)
        state = _state(audit, preview)
        atomic_write_json(work / "stage3_summary.json", summary)
        stage3_apply._write_report(work / "stage3_report.md", summary)
        atomic_write_json(work / STATE_NAME, state)
        _commit_metadata(input_dir, work)
        committed = True
        _verify_completed(input_dir, summary)
        return summary
    finally:
        if not committed:
            _remove_partial_metadata(input_dir)
        shutil.rmtree(work, ignore_errors=True)


def _summary(input_dir, audit, preview, records, payloads):
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION,
        "mode": "offline_fixed_evidence_streaming_reconstructed_metadata",
        "network_access": False, "packaging_version": PACKAGING_VERSION,
        "commits": _count_rows(input_dir / "commits.csv"),
        "episodes": audit.rows, "evidence_records": records,
        "evidence_payloads": payloads,
        "evidence_input_sha256": preview["evidence_input_sha256"],
        "registry_statuses": dict(audit.registry),
        "advisory_statuses": dict(audit.advisory),
        "stage2_episode_sha256": preview["stage2_episode_sha256"],
        "stage3_episode_sha256": audit.sha256,
        "protected_table_sha256": preview["protected_table_sha256"],
        "state_file": STATE_NAME,
        "invariants": _invariants(),
    }


def _state(audit: EpisodeAudit, preview) -> dict[str, object]:
    return {
        "state_schema_version": 1, "processed_groups": audit.rows,
        "data_complete": True, "complete": True, "stage": "rq2_stage3",
        "stage2_episode_sha256": preview["stage2_episode_sha256"],
        "evidence_input_sha256": preview["evidence_input_sha256"],
        "episodes": audit.rows, "csv_bytes": {"episodes": audit.bytes},
        "used_evidence_ids": list(audit.used_ids),
        "registry_statuses": dict(audit.registry),
        "advisory_statuses": dict(audit.advisory),
    }


def _invariants() -> dict[str, bool]:
    return {
        "network_access": False, "four_canonical_tables_only": True,
        "protected_tables_unchanged": True,
        "all_queryable_episodes_processed": True,
        "metadata_reconstructed_from_committed_stage3": True,
    }


def _commit_metadata(input_dir: Path, work: Path) -> None:
    for name in FINAL_OUTPUTS[:-1]:
        _replace_with_retry(work / name, input_dir / name)
    _replace_with_retry(work / STATE_NAME, input_dir / STATE_NAME)


def _verify_completed(input_dir: Path, expected) -> None:
    state = read_json(input_dir / STATE_NAME)
    summary = read_json(input_dir / "stage3_summary.json")
    if state.get("complete") is not True or summary != expected:
        raise RuntimeError("reconstructed Stage 3 metadata verification failed")


def _remove_partial_metadata(input_dir: Path) -> None:
    for name in FINAL_OUTPUTS:
        path = input_dir / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def _protected_hashes(input_dir: Path) -> dict[str, str]:
    names = ("commits.csv", "events.csv", "episode_event_links.csv")
    return {name: _sha256(input_dir / name) for name in names}


def _count_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _row in csv.DictReader(handle))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_names() -> tuple[str, ...]:
    return ("commits.csv", "events.csv", "episodes.csv", "episode_event_links.csv")


def main() -> None:
    args = parse_args()
    result = repair_stage3_metadata(
        args.input_dir, args.evidence_input, args.stage2_dir,
        args.stage2_state, apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

"""Apply offline RQ2 v2 Stage 3 evidence to dependency episodes.

[IN]: The four Stage 1-2 CSVs plus normalized offline evidence JSONL.
[OUT]: Atomically updated episodes.csv, four-table schema_manifest.json,
evidence.jsonl, content-addressed payload cache, summary, and Chinese report.
[POS]: Deterministic no-network Stage 3 entry point. It refuses incomplete
evidence, prior Stage 3 outputs, or partially enriched queryable episodes.
[SYNC]: Keep both rq2_stage3 policy modules, rq2_stage_schema.py,
scripts/OUTPUTS.md, scripts/CLAUDE.md, RQ2_STAGE3_EVIDENCE_INPUT.md, and
the Stage 4 runner and Phase 8 plan synchronized.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from rq2_stage3_advisory_evidence import (
    EvidenceBundle,
    EvidenceItem,
    advisory_query_key,
    build_evidence_bundle,
    evaluate_advisory,
)
from rq2_stage3_version_query import (
    PACKAGING_VERSION,
    evaluate_registry,
    registry_query_key,
)
from rq2_stage3_streaming import run_stage3_streaming
from rq2_stage_schema import (
    SCHEMA_VERSION,
    build_schema_manifest,
    table_contract,
    validate_row,
)


DEFAULT_INPUT = Path("data_products/rq2_stage1_v2")
STAGE3_TARGETS = (
    "evidence.jsonl", "stage3_evidence_cache",
    "stage3_summary.json", "stage3_report.md", "stage3_state.json",
)
EVIDENCE_FIELDS = (
    "evidence_id", "evidence_type", "query_key", "source", "lookup_status",
    "queried_at", "payload_sha256", "payload_path", "error", "schema_version",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--evidence-input", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=1000)
    parser.add_argument("--max-buffer-rows", type=int, default=10_000)
    return parser.parse_args()


def run_stage3(
    input_dir: Path, evidence_input: Path, *, resume: bool = False,
    checkpoint_every: int = 1000, max_buffer_rows: int = 10_000,
) -> dict[str, object]:
    return run_stage3_streaming(
        input_dir, evidence_input, enrich_episodes, _write_report,
        build_schema_manifest, resume=resume,
        checkpoint_every=checkpoint_every, max_buffer_rows=max_buffer_rows,
    )

def prepare_stage3(input_dir: Path, evidence_input: Path) -> None:
    required = [input_dir / name for name in (*table_contract_names(), "schema_manifest.json")]
    missing = [str(path) for path in required if not path.is_file()]
    if not evidence_input.is_file():
        missing.append(str(evidence_input))
    if missing:
        raise FileNotFoundError(f"missing Stage 3 inputs: {missing}")
    existing = [name for name in STAGE3_TARGETS if (input_dir / name).exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite Stage 3 outputs: {existing}")


def table_contract_names() -> tuple[str, ...]:
    return ("commits.csv", "events.csv", "episodes.csv", "episode_event_links.csv")


def read_jsonl(path: Path) -> list[dict[str, object]]:
    rows = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"evidence line {number} must be an object")
            rows.append(value)
    return rows


def _read_four_tables(directory: Path) -> dict[str, list[dict[str, str]]]:
    return {filename: _read_table(directory / filename) for filename in table_contract_names()}


def _read_table(path: Path) -> list[dict[str, str]]:
    contract = table_contract(path.name)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != contract.fields:
            raise ValueError(f"invalid header for {path.name}")
        rows = list(reader)
    for row in rows:
        validate_row(path.name, row)
    return rows


def enrich_episodes(
    episodes: Sequence[Mapping[str, str]], commits: Sequence[Mapping[str, str]],
    bundle: EvidenceBundle,
) -> tuple[list[dict[str, str]], set[str]]:
    author_dates = {row["commit_id"]: row["author_date"] for row in commits}
    updated, used = [], set()
    for episode in episodes:
        if episode["query_kind"] in {"excluded", "unresolved"}:
            updated.append(dict(episode))
            continue
        _require_stage2_state(episode)
        author_date = author_dates.get(episode["commit_id"])
        if author_date is None:
            raise ValueError(f"episode references unknown commit: {episode['episode_id']}")
        row, evidence_ids = _enrich_episode(episode, author_date, bundle)
        updated.append(row)
        used.update(evidence_ids)
    return updated, used


def _require_stage2_state(episode: Mapping[str, str]) -> None:
    if episode["registry_status"] != "not_queried" or episode["advisory_status"] != "not_queried":
        raise ValueError(f"queryable episode is not in Stage 2 state: {episode['episode_id']}")


def _enrich_episode(episode, author_date, bundle):
    registry_key = registry_query_key(episode["ecosystem"], episode["package_name"])
    advisory_key = advisory_query_key(
        episode["ecosystem"], episode["package_name"],
        episode["query_kind"], episode["query_value"],
    )
    registry_item = _required_evidence(bundle, registry_key)
    advisory_item = _required_evidence(bundle, advisory_key)
    registry = evaluate_registry(episode, author_date, registry_item.record)
    advisory = evaluate_advisory(author_date, advisory_item.record)
    reasons = _merge_tokens(episode["reason_codes"], _decision_reasons(registry, advisory))
    evidence_ids = (registry_item.evidence_id, advisory_item.evidence_id)
    row = {
        **episode, "registry_status": registry.status,
        "advisory_status": advisory.status, "reason_codes": reasons,
        "evidence_ids": _merge_tokens(episode["evidence_ids"], evidence_ids),
    }
    validate_row("episodes.csv", row)
    return row, evidence_ids


def _decision_reasons(registry, advisory) -> tuple[str, ...]:
    reasons = set((*registry.reasons, *advisory.reasons))
    historical = registry.status == "current_absent_unknown"
    historical = historical and advisory.status == "active_before_or_at_author_date"
    if historical:
        reasons.discard("unknown_deleted_or_unpublished_possible")
        reasons.add("historical_advisory_precedes_current_absence")
    return tuple(sorted(reasons))


def _required_evidence(bundle: EvidenceBundle, query_key: str) -> EvidenceItem:
    try:
        return bundle.by_query_key[query_key]
    except KeyError as exc:
        raise ValueError(f"missing offline evidence for query: {query_key}") from exc


def _merge_tokens(existing: str, added: Iterable[str]) -> str:
    values = {item for item in existing.split("|") if item}
    values.update(item for item in added if item)
    return "|".join(sorted(values))


def _prepare_staging(input_dir: Path) -> Path:
    path = input_dir / f".stage3.{os.getpid()}.tmp"
    if path.exists():
        raise FileExistsError(f"Stage 3 staging path exists: {path}")
    path.mkdir(parents=False)
    shutil.copyfile(input_dir / "episodes.csv", path / "episodes.original.csv")
    shutil.copyfile(input_dir / "schema_manifest.json", path / "schema_manifest.original.json")
    return path


def _write_staged_outputs(
    staging: Path, episodes: Sequence[Mapping[str, str]],
    bundle: EvidenceBundle, used: set[str],
) -> None:
    _write_table(staging / "episodes.csv", episodes)
    items = [item for item in bundle.items if item.evidence_id in used]
    _write_evidence_jsonl(staging / "evidence.jsonl", items)
    _write_payloads(staging, items)


def _write_table(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    contract = table_contract(path.name)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=contract.fields)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def _write_evidence_jsonl(path: Path, items: Sequence[EvidenceItem]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            if tuple(item.metadata) != EVIDENCE_FIELDS:
                raise ValueError("invalid evidence metadata fields")
            handle.write(json.dumps(item.metadata, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_payloads(staging: Path, items: Sequence[EvidenceItem]) -> None:
    (staging / "stage3_evidence_cache").mkdir(parents=True, exist_ok=True)
    for item in items:
        path = staging / str(item.metadata["payload_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != item.payload:
            raise ValueError(f"payload hash collision: {item.payload_sha256}")
        if not path.exists():
            path.write_bytes(item.payload)


def _build_summary(
    tables, updated, used, bundle, protected, staging, evidence_input
) -> dict[str, object]:
    statuses = Counter(row["registry_status"] for row in updated)
    advisories = Counter(row["advisory_status"] for row in updated)
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION, "mode": "offline_fixed_evidence",
        "network_access": False, "packaging_version": PACKAGING_VERSION,
        "commits": len(tables["commits.csv"]), "episodes": len(updated),
        "evidence_records": len(used),
        "evidence_payloads": _used_payload_count(bundle, used),
        "evidence_input_sha256": _sha256(evidence_input),
        "registry_statuses": dict(sorted(statuses.items())),
        "advisory_statuses": dict(sorted(advisories.items())),
        "stage2_episode_sha256": _sha256(staging.parent / "episodes.csv"),
        "stage3_episode_sha256": _sha256(staging / "episodes.csv"),
        "protected_table_sha256": protected,
        "invariants": {
            "network_access": False, "four_canonical_tables_only": True,
            "protected_tables_unchanged": True, "all_queryable_episodes_processed": True,
        },
    }


def _used_payload_count(bundle: EvidenceBundle, used: set[str]) -> int:
    return len({item.payload_sha256 for item in bundle.items if item.evidence_id in used})


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_report(path: Path, summary: Mapping[str, object]) -> None:
    lines = [
        "# RQ2 Stage 3 v2 Offline Evidence Report", "",
        "Generated by `rq2_stage3_apply.py` from fixed local evidence without network access.", "",
        f"- Episodes: {summary['episodes']}",
        f"- Evidence metadata: {summary['evidence_records']}",
        f"- Deduplicated payloads: {summary['evidence_payloads']}",
        f"- Registry statuses：`{json.dumps(summary['registry_statuses'], ensure_ascii=False)}`",
        f"- Advisory statuses：`{json.dumps(summary['advisory_statuses'], ensure_ascii=False)}`",
        f"- packaging: `{summary['packaging_version']}`", "",
        "Registry and advisory remain separate evidence axes; current absence does not imply never published.", "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _commit_staging(input_dir: Path, staging: Path) -> None:
    for name in ("evidence.jsonl", "stage3_evidence_cache", "stage3_summary.json", "stage3_report.md"):
        os.replace(staging / name, input_dir / name)
    os.replace(staging / "episodes.csv", input_dir / "episodes.csv")


def _finalize_staging(staging: Path) -> None:
    (staging / "episodes.original.csv").unlink()
    (staging / "schema_manifest.original.json").unlink()
    staging.rmdir()


def _cleanup_failed_stage(input_dir: Path, staging: Path, *, rollback: bool) -> None:
    if rollback and (staging / "episodes.original.csv").is_file():
        os.replace(staging / "episodes.original.csv", input_dir / "episodes.csv")
        os.replace(staging / "schema_manifest.original.json", input_dir / "schema_manifest.json")
    if staging.exists():
        shutil.rmtree(staging)
    for name in STAGE3_TARGETS:
        path = input_dir / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def _protected_hashes(input_dir: Path) -> dict[str, str]:
    names = ("commits.csv", "events.csv", "episode_event_links.csv")
    return {name: _sha256(input_dir / name) for name in names}


def _verify_protected(input_dir: Path, expected: Mapping[str, str]) -> None:
    if _protected_hashes(input_dir) != dict(expected):
        raise RuntimeError("Stage 3 modified a protected canonical table")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main() -> None:
    args = parse_args()
    summary = run_stage3(
        args.input_dir, args.evidence_input, resume=args.resume,
        checkpoint_every=args.checkpoint_every,
        max_buffer_rows=args.max_buffer_rows,
    )
    print(f"episodes={summary['episodes']}")
    print(f"evidence_records={summary['evidence_records']}")
    print(f"input_dir={args.input_dir}")


if __name__ == "__main__":
    main()

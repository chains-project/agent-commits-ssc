"""Correct adjudication rows sourced only from commented manifest lines.

[IN]: Adjudication CSV, candidate evidence CSV, and immutable local patches.
[OUT]: Corrected adjudication CSV, correction audit CSV, and JSON summary.
[POS]: Independent post-adjudication guard; final adjudicator inputs remain read-only.
[SYNC]: Keep tests/test_rq2_adjudication.py and scripts/CLAUDE.md aligned.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import uuid
from pathlib import Path
from typing import Mapping, Sequence


csv.field_size_limit(1_000_000_000)
SCRIPT_ROOT = Path(__file__).resolve().parents[2] / "hallucination"
sys.path.insert(0, str(SCRIPT_ROOT))
from stage1_manifest_diff import parse_unified_diff  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adjudication", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    return parser.parse_args()


def line_map(path: str) -> dict[tuple[str, int], str]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return {
        (patch.path, line.new_line_number): line.text
        for patch in parse_unified_diff(text.splitlines())
        for line in patch.lines
    }


def commented_only_episodes(path: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    cache: dict[str, dict[tuple[str, int], str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            correction = _commented_correction(row, cache)
            if correction is not None:
                result[row["episode_id"]] = correction
    return result


def _commented_correction(
    row: Mapping[str, str],
    cache: dict[str, dict[tuple[str, int], str]],
) -> dict[str, str] | None:
    events = [
        event for event in json.loads(row["events_json"])
        if event["event_type"] == "manifest_addition"
    ]
    if not events or not any(
        event["path"].lower().endswith("pyproject.toml") for event in events
    ):
        return None
    patch = row["patch_path"]
    if patch not in cache:
        cache[patch] = line_map(patch)
    lines = [
        cache[patch].get((event["path"], int(event["new_line_number"])), "")
        for event in events
    ]
    if not lines or not all(line.lstrip().startswith("#") for line in lines):
        return None
    return {"commented_lines_json": json.dumps(lines, ensure_ascii=False)}


def corrected_row(
    row: dict[str, str],
    correction: dict[str, str] | None,
) -> dict[str, str]:
    output = dict(row)
    if correction is None:
        return output
    flags = set(json.loads(output["evidence_flags_json"]))
    flags.add("commented_manifest_parser_false_positive")
    output["final_label"] = "not_hallucination"
    output["confidence"] = "high"
    output["adjudication_basis"] = (
        "manifest event was parsed from a commented-out dependency line"
    )
    output["evidence_flags_json"] = json.dumps(
        sorted(flags), ensure_ascii=False
    )
    return output


def write_atomic(
    path: Path,
    fields: Sequence[str],
    rows: Sequence[Mapping[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def apply_corrections(
    adjudication: Path,
    evidence: Path,
) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
    corrections = commented_only_episodes(evidence)
    with adjudication.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        source = list(reader)
    rows = [
        corrected_row(row, corrections.get(row["episode_id"]))
        for row in source
    ]
    changed = _changed_rows(source, rows, corrections)
    return fields, rows, changed


def _changed_rows(
    source: Sequence[Mapping[str, str]],
    rows: Sequence[Mapping[str, str]],
    corrections: Mapping[str, Mapping[str, str]],
) -> list[dict[str, str]]:
    return [
        {
            "episode_id": before["episode_id"],
            "old_label": before["final_label"],
            "new_label": after["final_label"],
            **corrections[before["episode_id"]],
        }
        for before, after in zip(source, rows)
        if before["episode_id"] in corrections
    ]


def main() -> None:
    args = parse_args()
    fields, rows, changed = apply_corrections(
        args.adjudication, args.evidence
    )
    write_atomic(args.output, fields, rows)
    audit_fields = [
        "episode_id", "old_label", "new_label", "commented_lines_json"
    ]
    write_atomic(args.audit, audit_fields, changed)
    summary = {
        "rows": len(rows),
        "commented_manifest_corrections": len(changed),
    }
    args.summary.write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()


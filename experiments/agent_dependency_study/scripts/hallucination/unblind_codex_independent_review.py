"""Compare locked Codex review with historical preliminary labels after unblinding.

[IN]: fingerprinted codex_review_form_locked.csv and historical
preliminary_codex_review.csv.
[OUT]: disagreement audit CSV, descriptive agreement summary, invariants, and
fingerprint manifest.
[POS]: Post-lock diagnostic only. Agreement is not independent inter-rater
reliability and this script never changes the locked review.
[SYNC]: Update scripts/OUTPUTS.md and the active review plan if outputs change.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from collect_codex_official_evidence import (
    DEFAULT_OUTPUT,
    fingerprint,
    load_rows,
    write_json,
)


DEFAULT_HISTORY = Path(
    "data_products/rq2_hallucinated_validation_20260721/preliminary_codex_review.csv"
)
AUDIT_FIELDS = (
    "case_id", "redacted_case_id", "repo", "sha", "package_name", "version",
    "locked_label", "historical_preliminary_label", "label_agreement",
    "locked_confidence", "historical_confidence", "locked_evidence_grade",
    "historical_evidence_grade", "locked_reason", "historical_reason_code",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--locked", type=Path, default=DEFAULT_OUTPUT / "codex_review_form_locked.csv"
    )
    parser.add_argument("--historical", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def note_value(notes: str, key: str) -> str:
    prefix = f"{key}="
    for part in notes.split(";"):
        value = part.strip()
        if value.startswith(prefix):
            return value[len(prefix):]
    return ""


def comparison_row(
    locked: dict[str, str], historical: dict[str, str]
) -> dict[str, str]:
    new_label = locked["label"]
    old_label = historical.get("preliminary_label", "")
    return {
        "case_id": locked["case_id"],
        "redacted_case_id": locked["redacted_case_id"],
        "repo": locked["repo"], "sha": locked["sha"],
        "package_name": locked["package_name"], "version": locked["version"],
        "locked_label": new_label, "historical_preliminary_label": old_label,
        "label_agreement": str(new_label == old_label).lower(),
        "locked_confidence": locked["confidence"],
        "historical_confidence": historical.get("confidence", ""),
        "locked_evidence_grade": note_value(locked.get("notes", ""), "evidence_grade"),
        "historical_evidence_grade": historical.get("evidence_grade", ""),
        "locked_reason": locked["reason"],
        "historical_reason_code": historical.get("reason_code", ""),
    }


def history_index(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row["case_id"]: row for row in rows}


def build_audit(
    locked: list[dict[str, str]], historical: list[dict[str, str]]
) -> list[dict[str, str]]:
    index = history_index(historical)
    return [
        comparison_row(row, index.get(row["case_id"], {}))
        for row in locked
    ]


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def transition_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    values = (
        f"{row['historical_preliminary_label']} -> {row['locked_label']}"
        for row in rows
    )
    return dict(Counter(values))


def audit_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    agreements = sum(row["label_agreement"] == "true" for row in rows)
    return {
        "cases": len(rows), "agreements": agreements,
        "disagreements": len(rows) - agreements,
        "descriptive_agreement_rate": agreements / len(rows) if rows else 0,
        "transitions": transition_counts(rows),
        "interpretation": (
            "Diagnostic only: both reviews are Codex-derived and are not "
            "independent human ratings; do not report as inter-rater reliability."
        ),
    }


def audit_invariants(
    rows: list[dict[str, str]], locked_before: dict[str, Any], locked: Path
) -> dict[str, Any]:
    missing = sum(not row["historical_preliminary_label"] for row in rows)
    changed = fingerprint(locked)["sha256"] != locked_before["sha256"]
    passed = len(rows) == 38 and missing == 0 and not changed
    return {
        "status": "pass" if passed else "fail", "rows": len(rows),
        "unique_case_ids": len({row["case_id"] for row in rows}),
        "missing_historical_joins": missing,
        "locked_review_changed": changed, "adjudicated_labels_written": 0,
    }


def manifest(
    args: argparse.Namespace, outputs: list[Path], invariants: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "inputs": {
            args.locked.name: fingerprint(args.locked),
            args.historical.name: fingerprint(args.historical),
        },
        "outputs": {path.name: fingerprint(path) for path in outputs},
        "invariants": invariants,
    }


def main() -> None:
    args = parse_args()
    locked_before = fingerprint(args.locked)
    locked = load_rows(args.locked)
    history = load_rows(args.historical)
    rows = build_audit(locked, history)
    audit_path = args.output_dir / "codex_review_disagreement_audit.csv"
    summary_path = args.output_dir / "codex_review_unblind_summary.json"
    invariant_path = args.output_dir / "codex_review_unblind_invariants.json"
    write_csv(audit_path, rows)
    write_json(summary_path, audit_summary(rows))
    invariants = audit_invariants(rows, locked_before, args.locked)
    write_json(invariant_path, invariants)
    output_paths = [audit_path, summary_path, invariant_path]
    write_json(
        args.output_dir / "codex_review_unblind_manifest.json",
        manifest(args, output_paths, invariants),
    )
    print(json.dumps({**invariants, **audit_summary(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

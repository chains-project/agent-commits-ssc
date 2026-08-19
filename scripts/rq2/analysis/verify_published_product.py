"""Verify the local public RQ2 data and validation surface without network access."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def adjudication_counts(path: Path) -> tuple[int, Counter[str], bool]:
    rows = 0
    labels: Counter[str] = Counter()
    portable = True
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            labels[row["final_label"]] += 1
            portable &= row["patch_path"].startswith("diff_corpus/")
    return rows, labels, portable


def add(checks: list[dict[str, object]], name: str, actual: object, expected: object) -> None:
    checks.append({
        "name": name,
        "passed": actual == expected,
        "actual": actual,
        "expected": expected,
    })


def verify(root: Path) -> dict[str, object]:
    checks: list[dict[str, object]] = []
    data = root / "data" / "rq2"
    results = root / "results"
    manifest = load_json(data / "public_product_manifest.json")
    csv_path = data / "v2_native_adjudication.csv"
    rows, labels, portable = adjudication_counts(csv_path)
    expected_labels = load_json(results / "summary/rq2/v2_final_summary.json")["label_counts"]

    add(checks, "adjudication_rows", rows, 74618)
    add(checks, "label_counts", dict(labels), expected_labels)
    add(checks, "portable_patch_paths", portable, True)
    add(
        checks, "adjudication_sha256", sha256(csv_path),
        manifest["public_files"]["data/rq2/v2_native_adjudication.csv"]["sha256"],
    )

    index = load_json(data / "canonical_table_index.json")
    table_count = sum(len(item["tables"]) for item in index["languages"].values())
    add(checks, "canonical_languages", len(index["languages"]), 6)
    add(checks, "canonical_tables", table_count, 24)
    add(checks, "canonical_paths_portable", all(
        item["selected_path"].startswith("external://")
        for item in index["languages"].values()
    ), True)

    verification = load_json(results / "validation/rq2/final_analysis_verification.json")
    stage4 = load_json(results / "validation/rq2/stage4_summary.json")
    adjudication = load_json(results / "validation/rq2/adjudication_synthetic_summary.json")
    equivalence = load_json(results / "validation/rq2/adjudication_equivalence_summary.json")
    add(checks, "final_analysis_status", verification["status"], "pass")
    add(checks, "final_analysis_checks", verification["check_count"], 80)
    add(checks, "stage4_passes", stage4["status_counts"].get("pass"), 49)
    add(checks, "adjudication_scenario_passes", adjudication["status_counts"].get("pass"), 9)
    add(checks, "adjudication_equivalence_passes", equivalence["status_counts"].get("pass"), 9)

    failed = [item["name"] for item in checks if not item["passed"]]
    return {
        "status": "pass" if not failed else "fail",
        "check_count": len(checks),
        "failed_checks": failed,
        "checks": checks,
    }


def main() -> None:
    report = verify(parse_args().root.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

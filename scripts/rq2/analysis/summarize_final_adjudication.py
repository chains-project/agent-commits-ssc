"""Summarize the published RQ2 final-adjudication CSV without changing it."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping


REQUIRED_FIELDS = {
    "repo", "sha", "agent", "ecosystem", "languages_json", "final_label",
    "confidence", "patch_path",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def read_rows(path: Path) -> Iterable[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_FIELDS - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"missing required fields: {sorted(missing)}")
        yield from reader


def language_values(value: str) -> list[str]:
    parsed = json.loads(value)
    if isinstance(parsed, str):
        return [parsed]
    return [str(item) for item in parsed]


def summarize(rows: Iterable[Mapping[str, str]]) -> dict[str, object]:
    labels: Counter[str] = Counter()
    agents: Counter[str] = Counter()
    ecosystems: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    commits: set[tuple[str, str]] = set()
    row_count = 0
    portable_paths = True
    for row in rows:
        row_count += 1
        labels[row["final_label"]] += 1
        agents[row["agent"]] += 1
        ecosystems[row["ecosystem"]] += 1
        commits.add((row["repo"], row["sha"]))
        languages.update(language_values(row["languages_json"]))
        portable_paths &= row["patch_path"].startswith("diff_corpus/")
    return {
        "schema_version": 1,
        "adjudication_rows": row_count,
        "unique_commits": len(commits),
        "label_counts": dict(sorted(labels.items())),
        "by_agent": dict(sorted(agents.items())),
        "by_ecosystem": dict(sorted(ecosystems.items())),
        "by_language": dict(sorted(languages.items())),
        "portable_patch_paths": portable_paths,
    }


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    payload = summarize(read_rows(args.input))
    if args.output:
        write_json(args.output, payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

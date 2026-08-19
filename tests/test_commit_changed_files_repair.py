"""Regression tests for repairing indexed commits missing changed-file rows."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts" / "metadata"
sys.path.insert(0, str(SCRIPT_DIR))

import repair_commit_json_changed_files as repair


class ChangedFilesRepairTests(unittest.TestCase):
    def commit_json(self) -> dict[str, object]:
        return {
            "repo": "owner/repo",
            "sha": "abc123",
            "repo_language": "Python",
            "commit_primary_language": "Python",
            "files": [
                {
                    "filename": "a.py",
                    "status": "added",
                    "file_language": "Python",
                    "file_language_source": "extension",
                    "additions": 1,
                    "deletions": 0,
                    "changes": 1,
                    "patch": "+x",
                },
                {
                    "filename": "b.txt",
                    "status": "modified",
                    "file_language": "Text",
                    "file_language_source": "extension",
                    "additions": 1,
                    "deletions": 1,
                    "changes": 2,
                },
            ],
        }

    def test_apply_repairs_once_and_second_run_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            json_path = root / "commit.json"
            csv_path = root / "changed_files.csv"
            json_path.write_text(json.dumps(self.commit_json()), encoding="utf-8")

            first = repair.repair_changed_files(json_path, csv_path, apply=True)
            second = repair.repair_changed_files(json_path, csv_path, apply=True)

            self.assertEqual(first["status"], "repaired")
            self.assertEqual(first["appended_rows"], 2)
            self.assertEqual(first["verified_rows"], 2)
            self.assertGreater(first["final_csv_bytes"], first["original_csv_bytes"])
            self.assertEqual(second["status"], "already_complete")
            self.assertEqual(second["verified_rows"], 2)
            self.assertEqual(second["final_csv_bytes"], second["original_csv_bytes"])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["sha"] for row in rows}, {"abc123"})

    def test_partial_existing_rows_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            json_path = root / "commit.json"
            csv_path = root / "changed_files.csv"
            json_path.write_text(json.dumps(self.commit_json()), encoding="utf-8")
            rows = repair.changed_file_rows(self.commit_json())
            repair.append_rows(csv_path, rows[:1])

            result = repair.repair_changed_files(json_path, csv_path, apply=True)

            self.assertEqual(result["status"], "partial_existing_refused")
            self.assertEqual(result["existing_rows"], 1)


if __name__ == "__main__":
    unittest.main()

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_DIR))

import rq2_stage2_build as stage2
import rq2_stage_schema as schema


def write_table(path, rows):
    contract = schema.table_contract(path.name)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=contract.fields)
        writer.writeheader()
        writer.writerows(rows)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Stage2BuildTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name)
        self.commit_id = schema.make_commit_id("owner/repo", "2" * 40)
        self.commits = [self._commit()]
        self.events = [self._event(0, "import", "left-pad"), self._event(1, "manifest_addition", "left-pad", "1.3.0")]
        write_table(self.output / "commits.csv", self.commits)
        write_table(self.output / "events.csv", self.events)
        manifest = schema.build_schema_manifest(self.output, ["commits.csv", "events.csv"])
        (self.output / "schema_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def _commit(self):
        return {
            "commit_id": self.commit_id, "repo": "owner/repo", "sha": "2" * 40,
            "author_date": "2026-07-01T00:00:00Z", "agent": "Codex",
            "repo_primary_language": "TypeScript", "actual_changed_languages": "TypeScript",
            "import_event_count": "1", "manifest_event_count": "1",
            "dependency_quadrant": "I+M+", "schema_version": "2",
        }

    def _event(self, ordinal, event_type, package, version=""):
        path = "src/index.ts" if event_type == "import" else "package.json"
        event_id = schema.make_event_id(self.commit_id, event_type, path, ordinal + 1, ordinal, package)
        return {
            "event_id": event_id, "commit_id": self.commit_id,
            "event_type": event_type, "path": path,
            "actual_language": "TypeScript", "ecosystem": "npm",
            "new_line_number": str(ordinal + 1), "event_ordinal": str(ordinal),
            "raw_target": package, "package_candidate": package,
            "version_spec": version,
            "event_status": "direct_mapping" if event_type == "import" else "direct_manifest",
            "reason_codes": "", "schema_version": "2",
        }

    def test_run_adds_two_tables_and_preserves_stage1(self):
        before = {name: sha256(self.output / name) for name in ("commits.csv", "events.csv")}
        summary = stage2.run_stage2(self.output)
        after = {name: sha256(self.output / name) for name in before}
        self.assertEqual(before, after)
        self.assertEqual(summary["episodes"], 1)
        self.assertEqual(summary["episode_event_links"], 2)
        manifest = json.loads((self.output / "schema_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["table_count"], 4)
        self.assertEqual(set(manifest["tables"]), set(schema.TABLE_CONTRACTS))

    def test_second_run_refuses_to_overwrite(self):
        stage2.run_stage2(self.output)
        with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
            stage2.run_stage2(self.output)

    def test_count_mismatch_is_rejected_before_outputs(self):
        self.commits[0]["import_event_count"] = "9"
        write_table(self.output / "commits.csv", self.commits)
        with self.assertRaisesRegex(ValueError, "event counts disagree"):
            stage2.run_stage2(self.output)
        self.assertFalse((self.output / "episodes.csv").exists())

    def test_missing_stage1_input_is_rejected(self):
        (self.output / "schema_manifest.json").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "missing Stage 1 inputs"):
            stage2.run_stage2(self.output)


if __name__ == "__main__":
    unittest.main()

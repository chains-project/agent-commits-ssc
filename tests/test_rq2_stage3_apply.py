import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_DIR))

import rq2_stage3_apply as stage3
import rq2_stage_schema as schema


AUTHOR = "2026-07-01T00:00:00Z"
QUERY_TIME = "2026-08-03T00:00:00Z"


def write_csv(path, rows):
    contract = schema.table_contract(path.name)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=contract.fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Stage3ApplyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.evidence = self.root / "evidence.jsonl"
        self.commit_id = schema.make_commit_id("owner/repo", "3" * 40)
        self._write_tables()
        self._write_evidence(self._evidence_rows())
        self.original_episodes = (self.output / "episodes.csv").read_bytes()
        self.original_manifest = (self.output / "schema_manifest.json").read_bytes()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_tables(self):
        commit = {
            "commit_id": self.commit_id, "repo": "owner/repo", "sha": "3" * 40,
            "author_date": AUTHOR, "agent": "Codex", "repo_primary_language": "TypeScript",
            "actual_changed_languages": "TypeScript", "import_event_count": "0",
            "manifest_event_count": "3", "dependency_quadrant": "I-M+", "schema_version": "2",
        }
        episodes, events, links = [], [], []
        queries = [("left-pad", "exact", "1.3.0"), ("left-pad", "range", "^2.0.0"), ("absent-pkg", "name_only", "")]
        for index, (package, kind, value) in enumerate(queries):
            event, episode, link = self._rows(index, package, kind, value)
            events.append(event)
            episodes.append(episode)
            links.append(link)
        write_csv(self.output / "commits.csv", [commit])
        write_csv(self.output / "events.csv", events)
        write_csv(self.output / "episodes.csv", episodes)
        write_csv(self.output / "episode_event_links.csv", links)
        manifest = schema.build_schema_manifest(self.output)
        (self.output / "schema_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def _rows(self, index, package, kind, value):
        path = "package.json"
        event_id = schema.make_event_id(self.commit_id, "manifest_addition", path, index + 1, index, package)
        episode_id = schema.make_episode_id(self.commit_id, "npm", package, kind, value)
        event = {
            "event_id": event_id, "commit_id": self.commit_id,
            "event_type": "manifest_addition", "path": path,
            "actual_language": "TypeScript", "ecosystem": "npm",
            "new_line_number": str(index + 1), "event_ordinal": str(index),
            "raw_target": package, "package_candidate": package, "version_spec": value,
            "event_status": "direct_manifest", "reason_codes": "", "schema_version": "2",
        }
        episode = {
            "episode_id": episode_id, "commit_id": self.commit_id, "ecosystem": "npm",
            "package_name": package, "dependency_quadrant": "I-M+",
            "alignment_status": "not_applicable_manifest_only", "query_kind": kind,
            "query_value": value, "resolution_source": "direct_manifest",
            "registry_status": "not_queried", "advisory_status": "not_queried",
            "reason_codes": "", "evidence_ids": "", "schema_version": "2",
        }
        link = {
            "episode_id": episode_id, "event_id": event_id,
            "link_method": "manifest_only", "schema_version": "2",
        }
        return event, episode, link

    def _evidence_rows(self):
        left_pad = self._registry("left-pad", "ok", [
            {"version": "1.3.0", "published_at": "2020-01-01T00:00:00Z"},
            {"version": "2.0.0", "published_at": "2027-01-01T00:00:00Z"},
        ])
        absent = self._registry("absent-pkg", "not_found", [])
        return [
            left_pad, absent,
            self._advisory("left-pad", "exact", "1.3.0"),
            self._advisory("left-pad", "range", "^2.0.0"),
            self._advisory("absent-pkg", "name_only", ""),
        ]

    @staticmethod
    def _registry(package, status, versions):
        return {
            "evidence_type": "registry", "source": "npm", "ecosystem": "npm",
            "package_name": package, "lookup_status": status,
            "queried_at": QUERY_TIME, "versions": versions,
        }

    @staticmethod
    def _advisory(package, kind, value):
        return {
            "evidence_type": "advisory", "source": "OSV", "ecosystem": "npm",
            "package_name": package, "query_kind": kind, "query_value": value,
            "lookup_status": "ok", "queried_at": QUERY_TIME, "advisories": [],
        }

    def _write_evidence(self, rows):
        text = "\n".join(json.dumps(row) for row in rows) + "\n"
        self.evidence.write_text(text, encoding="utf-8")

    def test_run_updates_only_episodes_and_non_tabular_outputs(self):
        protected_names = ("commits.csv", "events.csv", "episode_event_links.csv")
        before = {name: digest(self.output / name) for name in protected_names}
        summary = stage3.run_stage3(self.output, self.evidence)
        after = {name: digest(self.output / name) for name in protected_names}
        self.assertEqual(before, after)
        self.assertEqual(summary["evidence_records"], 5)
        episodes = read_csv(self.output / "episodes.csv")
        statuses = {(row["package_name"], row["query_kind"]): row["registry_status"] for row in episodes}
        self.assertEqual(statuses[("left-pad", "exact")], "exists_before_author_date")
        self.assertEqual(statuses[("left-pad", "range")], "published_after_author_date")
        self.assertEqual(statuses[("absent-pkg", "name_only")], "current_absent_unknown")
        self.assertTrue(all(row["advisory_status"] == "none_observed" for row in episodes))
        self.assertTrue(all(row["evidence_ids"] for row in episodes))
        self.assertEqual({path.name for path in self.output.glob("*.csv")}, set(schema.TABLE_CONTRACTS))
        manifest = json.loads((self.output / "schema_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["table_count"], 4)

    def test_active_advisory_replaces_weak_current_absence_reason(self):
        rows = self._evidence_rows()
        rows[-1]["advisories"] = [{
            "id": "SYNTHETIC-ACTIVE", "published": "2020-01-01T00:00:00Z",
            "withdrawn": "", "hydration_status": "ok",
        }]
        self._write_evidence(rows)
        stage3.run_stage3(self.output, self.evidence)
        absent = next(row for row in read_csv(self.output / "episodes.csv") if row["package_name"] == "absent-pkg")
        self.assertIn("historical_advisory_precedes_current_absence", absent["reason_codes"])
        self.assertIn("registry_package_not_found_at_query", absent["reason_codes"])
        self.assertNotIn("unknown_deleted_or_unpublished_possible", absent["reason_codes"])

    def test_second_run_refuses_to_overwrite(self):
        stage3.run_stage3(self.output, self.evidence)
        with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
            stage3.run_stage3(self.output, self.evidence)

    def test_empty_episode_set_creates_valid_empty_evidence_cache(self):
        commit = read_csv(self.output / "commits.csv")[0]
        commit["manifest_event_count"] = "0"
        write_csv(self.output / "commits.csv", [commit])
        for name in ("events.csv", "episodes.csv", "episode_event_links.csv"):
            write_csv(self.output / name, [])
        manifest = schema.build_schema_manifest(self.output)
        (self.output / "schema_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.evidence.write_text("", encoding="utf-8")
        summary = stage3.run_stage3(self.output, self.evidence)
        self.assertEqual(summary["episodes"], 0)
        self.assertTrue((self.output / "stage3_evidence_cache").is_dir())

    def test_missing_evidence_fails_before_writes(self):
        self._write_evidence(self._evidence_rows()[:-1])
        with self.assertRaisesRegex(ValueError, "missing offline evidence"):
            stage3.run_stage3(self.output, self.evidence)
        self.assertEqual((self.output / "episodes.csv").read_bytes(), self.original_episodes)
        self.assertFalse((self.output / "evidence.jsonl").exists())

    def test_post_commit_failure_rolls_back_episode_and_manifest(self):
        with mock.patch.object(stage3, "build_schema_manifest", side_effect=RuntimeError("forced")):
            with self.assertRaisesRegex(RuntimeError, "forced"):
                stage3.run_stage3(self.output, self.evidence)
        self.assertEqual((self.output / "episodes.csv").read_bytes(), self.original_episodes)
        self.assertEqual((self.output / "schema_manifest.json").read_bytes(), self.original_manifest)
        self.assertFalse((self.output / "evidence.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

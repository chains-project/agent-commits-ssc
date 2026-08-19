import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_DIR))

import rq2_stage2_build as stage2
import rq2_stage_schema as schema


def write_table(path, rows):
    fields = schema.table_contract(path.name).fields
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class Stage2StreamingResumeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name)
        commits, events = self._inputs()
        write_table(self.output / "commits.csv", commits)
        write_table(self.output / "events.csv", events)
        manifest = schema.build_schema_manifest(
            self.output, ["commits.csv", "events.csv"]
        )
        (self.output / "schema_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _inputs(self):
        commits, events = [], []
        for ordinal, sha_char in enumerate(("2", "4")):
            commit_id = schema.make_commit_id("owner/repo", sha_char * 40)
            commits.append(self._commit(commit_id, sha_char * 40))
            events.extend(self._events(commit_id, ordinal))
        return commits, events

    def _commit(self, commit_id, sha):
        return {
            "commit_id": commit_id, "repo": "owner/repo", "sha": sha,
            "author_date": "2026-07-01T00:00:00Z", "agent": "Codex",
            "repo_primary_language": "TypeScript",
            "actual_changed_languages": "TypeScript",
            "import_event_count": "1", "manifest_event_count": "1",
            "dependency_quadrant": "I+M+", "schema_version": "2",
        }

    def _events(self, commit_id, ordinal):
        package = f"left-pad-{ordinal}"
        return [
            self._event(commit_id, "import", "src/index.ts", package, "", 0),
            self._event(commit_id, "manifest_addition", "package.json", package, "1.3.0", 1),
        ]

    def _event(self, commit_id, event_type, path, package, version, ordinal):
        event_id = schema.make_event_id(
            commit_id, event_type, path, ordinal + 1, ordinal, package
        )
        return {
            "event_id": event_id, "commit_id": commit_id,
            "event_type": event_type, "path": path,
            "actual_language": "TypeScript", "ecosystem": "npm",
            "new_line_number": str(ordinal + 1), "event_ordinal": str(ordinal),
            "raw_target": package, "package_candidate": package,
            "version_spec": version,
            "event_status": "direct_mapping" if event_type == "import" else "direct_manifest",
            "reason_codes": "", "schema_version": "2",
        }

    def test_resume_after_interruption_has_no_duplicate_rows(self):
        original = stage2.build_episode_tables
        calls = 0

        def interrupted(commits, events, contexts=()):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated interruption")
            return original(commits, events, contexts)

        with mock.patch.object(stage2, "build_episode_tables", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                stage2.run_stage2(self.output, resume=True, checkpoint_every=1)

        partial = json.loads(
            (self.output / "stage2_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(partial["processed_groups"], 1)
        summary = stage2.run_stage2(
            self.output, resume=True, checkpoint_every=1
        )
        self.assertEqual(summary["commits"], 2)
        self.assertEqual(summary["events"], 4)
        self.assertEqual(len(read_rows(self.output / "episodes.csv")), 2)
        self.assertEqual(len(read_rows(self.output / "episode_event_links.csv")), 4)
        complete = json.loads(
            (self.output / "stage2_state.json").read_text(encoding="utf-8")
        )
        self.assertTrue(complete["complete"])


if __name__ == "__main__":
    unittest.main()

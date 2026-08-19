import csv
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_DIR))

import rq2_stage3_apply as stage3
import repair_rq2_stage3_metadata as repair
import rq2_stage_schema as schema
import rq2_stage3_streaming as streaming


AUTHOR = "2026-07-01T00:00:00Z"
QUERY_TIME = "2026-08-03T00:00:00Z"


def write_csv(path, rows):
    fields = schema.table_contract(path.name).fields
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class Stage3StreamingResumeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.evidence = self.root / "evidence.jsonl"
        self.commit_id = schema.make_commit_id("owner/repo", "5" * 40)
        self._write_tables()
        self._write_evidence()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_tables(self):
        commit = {
            "commit_id": self.commit_id, "repo": "owner/repo", "sha": "5" * 40,
            "author_date": AUTHOR, "agent": "Codex",
            "repo_primary_language": "TypeScript",
            "actual_changed_languages": "TypeScript", "import_event_count": "0",
            "manifest_event_count": "2", "dependency_quadrant": "I-M+",
            "schema_version": "2",
        }
        events, episodes, links = [], [], []
        for ordinal, package in enumerate(("left-pad", "absent-pkg")):
            event, episode, link = self._rows(ordinal, package)
            events.append(event)
            episodes.append(episode)
            links.append(link)
        write_csv(self.output / "commits.csv", [commit])
        write_csv(self.output / "events.csv", events)
        write_csv(self.output / "episodes.csv", episodes)
        write_csv(self.output / "episode_event_links.csv", links)
        manifest = schema.build_schema_manifest(self.output)
        (self.output / "schema_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def _rows(self, ordinal, package):
        event_id = schema.make_event_id(
            self.commit_id, "manifest_addition", "package.json",
            ordinal + 1, ordinal, package,
        )
        episode_id = schema.make_episode_id(
            self.commit_id, "npm", package, "exact", "1.0.0")
        event = {
            "event_id": event_id, "commit_id": self.commit_id,
            "event_type": "manifest_addition", "path": "package.json",
            "actual_language": "TypeScript", "ecosystem": "npm",
            "new_line_number": str(ordinal + 1), "event_ordinal": str(ordinal),
            "raw_target": package, "package_candidate": package,
            "version_spec": "1.0.0", "event_status": "direct_manifest",
            "reason_codes": "", "schema_version": "2",
        }
        episode = {
            "episode_id": episode_id, "commit_id": self.commit_id,
            "ecosystem": "npm", "package_name": package,
            "dependency_quadrant": "I-M+",
            "alignment_status": "not_applicable_manifest_only",
            "query_kind": "exact", "query_value": "1.0.0",
            "resolution_source": "direct_manifest",
            "registry_status": "not_queried", "advisory_status": "not_queried",
            "reason_codes": "", "evidence_ids": "", "schema_version": "2",
        }
        return event, episode, {
            "episode_id": episode_id, "event_id": event_id,
            "link_method": "manifest_only", "schema_version": "2",
        }

    def _assert_completed(self, summary):
        self.assertEqual(summary["episodes"], 2)
        self.assertEqual(len(read_csv(self.output / "episodes.csv")), 2)
        complete = json.loads(
            (self.output / "stage3_state.json").read_text(encoding="utf-8")
        )
        self.assertTrue(complete["complete"])
        self.assertFalse((self.output / ".stage3_apply_work").exists())
        self.assertEqual(
            stage3.run_stage3(self.output, self.evidence, resume=True), summary
        )

    def _write_evidence(self):
        rows = []
        for package in ("left-pad", "absent-pkg"):
            status = "ok" if package == "left-pad" else "not_found"
            versions = [{"version": "1.0.0", "published_at": "2020-01-01T00:00:00Z"}]
            rows.append({
                "evidence_type": "registry", "source": "npm",
                "ecosystem": "npm", "package_name": package,
                "lookup_status": status, "queried_at": QUERY_TIME,
                "versions": versions if status == "ok" else [],
            })
            rows.append({
                "evidence_type": "advisory", "source": "OSV",
                "ecosystem": "npm", "package_name": package,
                "query_kind": "exact", "query_value": "1.0.0",
                "lookup_status": "ok", "queried_at": QUERY_TIME,
                "advisories": [],
            })
        self.evidence.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )

    def test_resume_after_interruption_has_no_duplicate_rows(self):
        original = stage3.enrich_episodes
        calls = 0

        def interrupted(episodes, commits, bundle):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated interruption")
            return original(episodes, commits, bundle)

        with mock.patch.object(stage3, "enrich_episodes", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                stage3.run_stage3(
                    self.output, self.evidence, resume=True, checkpoint_every=1
                )
        state_path = self.output / ".stage3_apply_work" / "stage3_state.json"
        partial = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(partial["processed_groups"], 1)

        summary = stage3.run_stage3(
            self.output, self.evidence, resume=True, checkpoint_every=1
        )
        self._assert_completed(summary)

    def test_stale_checkpoint_temp_does_not_break_final_cleanup(self):
        original = streaming._copy_rollback_inputs

        def copy_with_stale_temp(input_dir, work):
            original(input_dir, work)
            stale = work / ".stage3_state.json.999.tmp"
            stale.write_text("{}", encoding="utf-8")

        with mock.patch.object(
            streaming, "_copy_rollback_inputs", side_effect=copy_with_stale_temp
        ):
            summary = stage3.run_stage3(self.output, self.evidence, resume=True)
        self.assertEqual(summary["episodes"], 2)
        self.assertTrue((self.output / "stage3_state.json").is_file())
        self.assertFalse((self.output / streaming.WORK_NAME).exists())

    def test_completed_resume_rejects_tampered_canonical_episodes(self):
        stage3.run_stage3(self.output, self.evidence, resume=True)
        with (self.output / "episodes.csv").open("ab") as handle:
            handle.write(b"tampered")

        with self.assertRaisesRegex(RuntimeError, "completed Stage 3 episodes"):
            stage3.run_stage3(self.output, self.evidence, resume=True)

    def test_repair_rebuilds_missing_metadata_from_enriched_episodes(self):
        baseline = self.root / "stage2_baseline"
        baseline.mkdir()
        for name in ("episodes.csv", "episode_event_links.csv"):
            shutil.copyfile(self.output / name, baseline / name)
        stage2_state = baseline / "stage2_state.json"
        stage2_state.write_text(json.dumps({
            "episodes": 2, "episode_event_links": 2,
            "csv_bytes": {
                "episodes": (baseline / "episodes.csv").stat().st_size,
                "links": (baseline / "episode_event_links.csv").stat().st_size,
            },
        }), encoding="utf-8")
        stage3.run_stage3(self.output, self.evidence, resume=True)
        for name in streaming.FINAL_OUTPUTS:
            path = self.output / name
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        summary = repair.repair_stage3_metadata(
            self.output, self.evidence, baseline, stage2_state, apply=True
        )
        self.assertEqual(summary["episodes"], 2)
        state = json.loads((self.output / streaming.STATE_NAME).read_text())
        self.assertTrue(state["complete"])
        self.assertEqual(stage3.run_stage3(self.output, self.evidence, resume=True), summary)


if __name__ == "__main__":
    unittest.main()

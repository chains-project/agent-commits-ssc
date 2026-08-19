import csv
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_DIR))

import rq2_stage1_extract as stage1
import rq2_stage_schema as schema


def resource_exhausted_error():
    error = OSError("transient system resource exhaustion")
    error.winerror = 1450
    return error


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_patch(path, source_path, added_lines):
    lines = [
        f"diff --git a/{source_path} b/{source_path}",
        f"--- a/{source_path}",
        f"+++ b/{source_path}",
        f"@@ -0,0 +1,{len(added_lines)} @@",
    ]
    lines.extend(f"+{line}" for line in added_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Stage1ExtractTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.index = self.root / "commit_index.csv"
        self.population = self.root / "population.csv"
        self.output = self.root / "output"
        self.sha_values = [f"{index:040x}" for index in range(1, 5)]
        self._write_fixture()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_fixture(self):
        patches = [self.root / f"commit-{index}.diff" for index in range(4)]
        write_patch(patches[0], "tools/check.py", ["import requests"])
        self._append_patch(patches[0], "requirements.txt", ["requests==2.32.3"])
        write_patch(patches[1], "package.json", ['"dependencies": {', '"left-pad": "1.3.0"', '}'])
        write_patch(patches[2], "src/index.ts", ["import fs from 'fs';"])
        write_patch(patches[3], "java/src/App.java", ["import org.example.Widget;"])
        self._write_index(patches)
        self._write_population()

    def _write_index(self, patches):
        languages = ["TypeScript", "TypeScript", "TypeScript", "Ruby"]
        changed = ["Python", "JSON", "TypeScript", "Java"]
        rows = [
            {
                "status": "ok", "repo": "owner/repo", "sha": self.sha_values[index],
                "repo_language": languages[index], "commit_primary_language": languages[index],
                "commit_languages": changed[index],
                "patch_path": str(patch_path),
            }
            for index, patch_path in enumerate(patches)
        ]
        write_csv(
            self.index,
            ["status", "repo", "sha", "repo_language", "commit_primary_language", "commit_languages", "patch_path"],
            rows,
        )

    def _write_population(self):
        rows = [
            {
                "repo_sha": f"owner/repo|{sha}", "repo": "owner/repo", "sha": sha,
                "agent": "Codex", "author_date": "2026-07-01T00:00:00Z",
            }
            for sha in self.sha_values
        ]
        write_csv(
            self.population,
            ["repo_sha", "repo", "sha", "agent", "author_date"],
            rows,
        )
    @staticmethod
    def _append_patch(path, source_path, added_lines):
        lines = [
            f"diff --git a/{source_path} b/{source_path}",
            f"--- a/{source_path}",
            f"+++ b/{source_path}",
            f"@@ -0,0 +1,{len(added_lines)} @@",
        ]
        lines.extend(f"+{line}" for line in added_lines)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")

    def test_run_outputs_compact_stage1_tables(self):
        summary = stage1.run_stage1(self._config())
        commits = self._read_rows("commits.csv")
        events = self._read_rows("events.csv")
        self.assertEqual(summary["candidate_commits"], 2)
        self.assertEqual(summary["events"], 3)
        self.assertEqual({row["dependency_quadrant"] for row in commits}, {"I+M+", "I-M+"})
        self.assertEqual({row["event_type"] for row in events}, {"import", "manifest_addition"})
        self.assertFalse((self.output / "episodes.csv").exists())
        self.assertFalse((self.output / "episode_event_links.csv").exists())

    def test_mixed_language_uses_changed_source_path(self):
        stage1.run_stage1(self._config())
        commits = self._read_rows("commits.csv")
        mixed = next(row for row in commits if row["sha"] == self.sha_values[0])
        self.assertEqual(mixed["repo_primary_language"], "TypeScript")
        self.assertEqual(mixed["actual_changed_languages"], "Python")

    def test_rows_match_shared_contract_and_manifest(self):
        stage1.run_stage1(self._config())
        for filename in ("commits.csv", "events.csv"):
            for row in self._read_rows(filename):
                schema.validate_row(filename, row)
        manifest = json.loads((self.output / "schema_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["table_count"], 2)
        self.assertEqual(set(manifest["tables"]), {"commits.csv", "events.csv"})

    def test_scan_all_repo_languages_finds_java_in_ruby_repo(self):
        config = self._config(scan_all_repo_languages=True)
        summary = stage1.run_stage1(config)
        self.assertEqual(summary["candidate_commits"], 3)
        events = self._read_rows("events.csv")
        java = next(row for row in events if row["actual_language"] == "Java")
        self.assertEqual(java["event_status"], "mapping_uncertain")

    def test_actual_language_filter_keeps_matching_manifest_context(self):
        config = replace(
            self._config(scan_all_repo_languages=True),
            actual_languages=("Python",),
        )
        summary = stage1.run_stage1(config)
        self.assertEqual(summary["candidate_commits"], 1)
        events = self._read_rows("events.csv")
        self.assertEqual({row["event_type"] for row in events}, {"import", "manifest_addition"})
        self.assertEqual(self._read_rows("commits.csv")[0]["actual_changed_languages"], "Python")

    def test_actual_language_filter_routes_patch_files_not_index_hints(self):
        patches = [self.root / "tsx.diff", self.root / "manifest.diff"]
        write_patch(patches[0], "src/App.tsx", ["import x from 'left-pad';"])
        write_patch(
            patches[1], "package.json",
            ['"dependencies": {', '"left-pad": "1.3.0"', '}'],
        )
        rows = [
            {
                "status": "ok", "repo": "owner/repo", "sha": self.sha_values[index],
                "repo_language": "Python", "commit_primary_language": hint,
                "commit_languages": hint, "patch_path": str(patches[index]),
            }
            for index, hint in enumerate(("TSX", "JSON"))
        ]
        write_csv(
            self.index,
            ["status", "repo", "sha", "repo_language", "commit_primary_language", "commit_languages", "patch_path"],
            rows,
        )
        config = replace(
            self._config(scan_all_repo_languages=True),
            actual_languages=("TypeScript",),
        )
        summary = stage1.run_stage1(config)
        events = self._read_rows("events.csv")
        self.assertEqual(summary["candidate_commits"], 2)
        self.assertEqual({row["event_type"] for row in events}, {"import", "manifest_addition"})
        self.assertEqual(
            {row["actual_language"] for row in events},
            {"TypeScript", "JSON"},
        )

    def test_state_records_completed_stream(self):
        stage1.run_stage1(self._config())
        state = json.loads((self.output / "stage1_state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["data_complete"])
        self.assertTrue(state["complete"])
        self.assertEqual(state["candidate_commits"], 2)
        self.assertEqual(state["events"], 3)

    def test_resume_continues_after_last_committed_index_group(self):
        config = replace(self._config(), resume=True, checkpoint_every=1)
        original = stage1.extract_commit
        calls = 0

        def flaky(row, meta):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("forced interruption")
            return original(row, meta)

        with mock.patch.object(stage1, "extract_commit", side_effect=flaky):
            with self.assertRaisesRegex(RuntimeError, "forced interruption"):
                stage1.run_stage1(config)
        partial = json.loads((self.output / "stage1_state.json").read_text(encoding="utf-8"))
        self.assertEqual(partial["processed_groups"], 1)
        summary = stage1.run_stage1(config)
        self.assertEqual(summary["candidate_commits"], 2)
        self.assertEqual(len(self._read_rows("commits.csv")), 2)
        self.assertEqual(len(self._read_rows("events.csv")), 3)

    def test_patch_stat_retries_transient_winerror_1450(self):
        row = next(stage1.iter_index_rows(self.index, 1))
        original = Path.is_file
        calls = 0

        def flaky(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise resource_exhausted_error()
            return original(path)

        with mock.patch.object(Path, "is_file", new=flaky):
            extraction = stage1.extract_commit(row, {})
        self.assertIsNotNone(extraction)
        self.assertEqual(calls, 2)

    def test_patch_open_retries_transient_winerror_1450(self):
        patch_path = self.root / "commit-0.diff"
        original = Path.open
        calls = 0

        def flaky(path, *args, **kwargs):
            nonlocal calls
            calls += 1
            if path == patch_path and calls == 1:
                raise resource_exhausted_error()
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", new=flaky):
            lines = list(stage1.clean_lines(patch_path))
        self.assertTrue(lines)
        self.assertEqual(calls, 2)

    def test_patch_stat_does_not_retry_other_os_errors(self):
        row = next(stage1.iter_index_rows(self.index, 1))
        failure = OSError("ordinary I/O failure")
        with mock.patch.object(Path, "is_file", side_effect=failure) as stat:
            with self.assertRaisesRegex(OSError, "ordinary I/O failure"):
                stage1.extract_commit(row, {})
        self.assertEqual(stat.call_count, 1)

    def test_patch_stat_winerror_1450_retry_is_bounded(self):
        row = next(stage1.iter_index_rows(self.index, 1))
        failure = resource_exhausted_error()
        with mock.patch.object(Path, "is_file", side_effect=failure) as stat:
            with mock.patch.object(stage1.time, "sleep") as sleep:
                with self.assertRaisesRegex(OSError, "resource exhaustion"):
                    stage1.extract_commit(row, {})
        self.assertEqual(stat.call_count, stage1.RESOURCE_RETRY_ATTEMPTS)
        self.assertEqual(sleep.call_count, stage1.RESOURCE_RETRY_ATTEMPTS - 1)

    def test_non_empty_output_is_not_overwritten(self):
        self.output.mkdir()
        (self.output / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
            stage1.run_stage1(self._config())
        self.assertEqual((self.output / "keep.txt").read_text(encoding="utf-8"), "keep")

    def _config(self, scan_all_repo_languages=False):
        return stage1.Stage1Config(
            commit_index=self.index,
            population_csv=self.population,
            output_dir=self.output,
            scan_all_repo_languages=scan_all_repo_languages,
            sample_per_repo_language=0,
        )

    def _read_rows(self, filename):
        with (self.output / filename).open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))


if __name__ == "__main__":
    unittest.main()

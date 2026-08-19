import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_DIR))

import rq2_stage2_context as context
import rq2_stage2_episode_linker as linker
import rq2_stage_schema as schema
import run_rq2_phase9_experiment as phase9


class Stage2ContextTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = "owner/repo"
        self.sha = "6" * 40
        self.commit_id = schema.make_commit_id(self.repo, self.sha)

    def tearDown(self):
        self.temporary.cleanup()

    def test_go_repository_import_is_derived_as_first_party(self):
        commit = {"repo": "owner/project", "sha": "a" * 40}
        event = {
            "ecosystem": "Go", "path": "internal/app.go",
            "raw_target": "github.com/owner/project/internal/config",
        }
        rows = context.derived_context_rows(commit, [event])
        self.assertEqual(rows[0]["alignment_status"], "first_party_excluded")
        self.assertEqual(rows[0]["resolution_source"], "repository_module_identity")

    def test_go_parsed_module_context_handles_fork_identity(self):
        commit = self._commit("1", "0")
        event = self._go_event(
            "github.com/upstream/project/internal/config", "internal/app.go"
        )
        module = context.legacy_dependency_to_context(self._go_dependency(
            "github.com/upstream/project", source="first_party_module",
        ))
        episodes, _links = linker.build_episode_tables([commit], [event], [module])
        self.assertEqual(episodes[0]["package_name"], "github.com/upstream/project")
        self.assertEqual(episodes[0]["alignment_status"], "first_party_excluded")
        self.assertEqual(episodes[0]["query_kind"], "excluded")

    def test_go_parsed_dependency_maps_subpackage_to_module_version(self):
        commit = self._commit("1", "0")
        event = self._go_event("github.com/stretchr/testify/assert", "cmd/main.go")
        dependency = context.legacy_dependency_to_context(self._go_dependency(
            "github.com/stretchr/testify", version="v1.9.0",
        ))
        episodes, _links = linker.build_episode_tables(
            [commit], [event], [dependency]
        )
        self.assertEqual(episodes[0]["package_name"], "github.com/stretchr/testify")
        self.assertEqual((episodes[0]["query_kind"], episodes[0]["query_value"]), ("exact", "v1.9.0"))

    def test_legacy_cargo_workspace_dependency_is_excluded(self):
        row = {
            "repo": self.repo, "sha": self.sha, "repo_language": "Rust",
            "dep_file_path": "crates/tui/Cargo.toml",
            "package_name": "ratatui",
            "version_spec": "{ workspace = true, features = [",
            "resolved_version": "", "resolution_source": "manifest",
            "dependency_group": "dependencies",
        }
        converted = context.legacy_dependency_to_context(row)
        self.assertEqual(converted["version_spec"], "workspace:")
        self.assertEqual(converted["alignment_status"], "private_or_local_excluded")
        self.assertIn("workspace_inherited", converted["reason_codes"])

    def test_legacy_cargo_workspace_match_is_excluded(self):
        row = self._legacy("observed_in_parsed_manifest", ecosystem="Cargo")
        row.update({
            "declared_package": "ratatui",
            "version_spec": "{ workspace = true, features = [",
            "resolved_version": "",
        })
        converted = context.legacy_match_to_context(row)
        self.assertEqual(converted["version_spec"], "workspace:")
        self.assertEqual(converted["alignment_status"], "private_or_local_excluded")
        self.assertIn("workspace_inherited", converted["reason_codes"])

    def test_go_nested_module_context_prefers_nearest_longest_prefix(self):
        commit = self._commit("1", "0")
        event = self._go_event(
            "github.com/owner/project/cli/internal/config", "cli/internal/app.go"
        )
        rows = [
            context.legacy_dependency_to_context(self._go_dependency(
                "github.com/owner/project", source="first_party_module",
            )),
            context.legacy_dependency_to_context(self._go_dependency(
                "github.com/owner/project/cli", source="first_party_module",
                path="cli/go.mod",
            )),
        ]
        episodes, _links = linker.build_episode_tables([commit], [event], rows)
        self.assertEqual(episodes[0]["package_name"], "github.com/owner/project/cli")
        self.assertEqual(episodes[0]["query_kind"], "excluded")

    def test_legacy_rows_convert_first_party_lock_and_unknown(self):
        first = context.legacy_match_to_context(
            self._legacy("first_party_module_import", ecosystem="PyPI")
        )
        self.assertEqual(first["alignment_status"], "first_party_excluded")
        self.assertEqual(first["reason_codes"], "first_party")
        lock = context.legacy_match_to_context(
            self._legacy("observed_in_lockfile_possible_transitive")
        )
        self.assertEqual(lock["alignment_status"], "lockfile_only_observed")
        self.assertIn("possible_transitive_dependency", lock["reason_codes"])
        unknown = context.legacy_match_to_context(
            self._legacy("mapping_unknown", ecosystem="PyPI")
        )
        self.assertIn("python_alias_not_found", unknown["reason_codes"])

    def test_legacy_wheel_match_keeps_provenance(self):
        row = self._legacy("observed_in_python_wheel_metadata", ecosystem="PyPI")
        row["declared_package"] = "opencv-python"
        converted = context.legacy_match_to_context(row)
        self.assertEqual(converted["resolution_source"], "python_wheel_metadata")
        self.assertIn("python_wheel_metadata", converted["reason_codes"])
        missing = context.legacy_match_to_context(
            self._legacy("not_observed_in_parsed_dependency_files", ecosystem="PyPI")
        )
        self.assertEqual(missing["alignment_status"], "mapping_uncertain")
        self.assertIn("python_alias_not_found", missing["reason_codes"])

    def test_python_wheel_sources_build_bridge_and_preserve_failures(self):
        database, summary = self._build_python_wheel_database()
        first_sha = "6" * 40
        second_sha = "7" * 40
        self.assertEqual(summary["python_wheel_statuses"], {"no_wheel": 1, "ok": 4})
        self.assertEqual(len(summary["source_manifest"][-1]["sha256"]), 64)
        unique, ambiguous = self._lookup_contexts(database, first_sha, second_sha)
        self._assert_wheel_bridges(unique, ambiguous)

    def _assert_wheel_bridges(self, unique, ambiguous):
        bridge = [row for row in unique if row["source_path"] == "*"][0]
        self.assertEqual(bridge["package_name"], "opencv-python")
        self.assertIn("python_wheel_metadata", bridge["reason_codes"])
        conflict = [row for row in ambiguous if row["source_path"] == "*"][0]
        self.assertEqual(conflict["alignment_status"], "mapping_uncertain")
        self.assertEqual(conflict["package_name"], "")
        self.assertIn("python_wheel_multiple_candidates", conflict["reason_codes"])

    def test_wheel_bridge_beats_exact_negative_context(self):
        commit = self._commit("1", "0")
        event = self._python_event()
        negative, bridge = self._wheel_context_rows()
        episodes, _links = linker.build_episode_tables(
            [commit], [event], [negative, bridge]
        )
        self.assertEqual(episodes[0]["package_name"], "opencv-python")
        self.assertEqual(episodes[0]["query_kind"], "exact")
        self.assertEqual(episodes[0]["query_value"], "4.10.0.84")
        self.assertEqual(episodes[0]["resolution_source"], "python_wheel_metadata")
        self.assertIn("python_wheel_metadata", episodes[0]["reason_codes"])

    def test_sqlite_database_is_reused_and_lookup_is_scoped(self):
        source = self.root / "matches.csv"
        rows = [self._legacy("observed_in_parsed_manifest")]
        with source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0])
            writer.writeheader()
            writer.writerows(rows)
        database = self.root / "context.sqlite"
        spec = [context.ContextSource("TypeScript", source)]
        first = context.build_legacy_context_database(database, spec)
        second = context.build_legacy_context_database(database, spec)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        lookup = context.SqliteContextLookup(database)
        try:
            found = lookup.lookup({"repo": self.repo, "sha": self.sha})
        finally:
            lookup.close()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["package_name"], "left-pad")

    def test_legacy_sqlite_without_wheel_tables_remains_reusable(self):
        source = self.root / "matches.csv"
        self._write_csv(source, [self._legacy("observed_in_parsed_manifest")])
        spec = [context.ContextSource("TypeScript", source)]
        database = self.root / "legacy.sqlite"
        connection = sqlite3.connect(database)
        fields = ",".join(f"{field} TEXT NOT NULL" for field in context.CONTEXT_FIELDS)
        connection.execute(f"CREATE TABLE contexts (repo_key TEXT NOT NULL,{fields})")
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        manifest = context._source_manifest(spec)
        connection.execute("INSERT INTO meta VALUES (?,?)", ("source_manifest", json.dumps(manifest, sort_keys=True)))
        connection.execute("INSERT INTO meta VALUES (?,?)", ("rows_seen", "1"))
        connection.commit()
        connection.close()
        summary = context.build_legacy_context_database(database, spec)
        self.assertTrue(summary["reused"])
        self.assertNotIn("python_wheel_statuses", summary)

    def test_phase9_python_context_sources_include_direct_wheel_inputs(self):
        wheel = self.root / "python_wheel_import_map.csv"
        sources = phase9._context_sources(self.root, "Python", wheel)
        self.assertEqual(
            [source.kind for source in sources],
            ["import_matches", "python_dependencies", "python_wheel_map"],
        )
        self.assertEqual(sources[-1].path, wheel)

    def test_phase9_go_context_sources_include_parsed_dependencies(self):
        sources = phase9._context_sources(self.root, "Go", self.root / "unused.csv")
        self.assertEqual(
            [source.kind for source in sources],
            ["import_matches", "parsed_dependencies"],
        )

    def test_phase9_java_collector_uses_configured_maven_workers(self):
        args = SimpleNamespace(
            v2_cache_root=self.root / "v2",
            legacy_cache_root=self.root / "legacy-cache",
            legacy_root=self.root / "legacy",
            timeout=25,
            maven_workers=2,
        )
        with mock.patch.object(phase9, "run_collector", return_value={}) as run:
            phase9._collect(args, self.root / "Java")
        self.assertEqual(run.call_args.kwargs["maven_workers"], 2)

    def test_context_refines_lock_and_keeps_import_only_quadrant(self):
        commit = self._commit("1", "0")
        event = self._event("import", "src/index.ts", "left-pad", "")
        rows = [self._context("lockfile_only_observed", resolved="1.3.0")]
        episodes, links = linker.build_episode_tables([commit], [event], rows)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["dependency_quadrant"], "I+M-")
        self.assertEqual(episodes[0]["query_kind"], "exact")
        self.assertEqual(episodes[0]["query_value"], "1.3.0")
        self.assertEqual(episodes[0]["alignment_status"], "lockfile_only_observed")
        self.assertEqual(len(links), 1)

    def test_context_refines_direct_manifest_without_fake_link(self):
        commit = self._commit("1", "1")
        imported = self._event("import", "src/index.ts", "left-pad", "")
        manifest = self._event(
            "manifest_addition", "package.json", "left-pad", "^1.3.0", ordinal=1
        )
        rows = [self._context("matched_manifest_and_lock", resolved="1.3.0")]
        episodes, links = linker.build_episode_tables(
            [commit], [imported, manifest], rows
        )
        self.assertEqual(episodes[0]["dependency_quadrant"], "I+M+")
        self.assertEqual(episodes[0]["query_kind"], "exact")
        self.assertEqual(episodes[0]["alignment_status"], "matched_manifest_and_lock")
        self.assertEqual(len(links), 2)

    def _legacy(self, status, ecosystem="npm"):
        return {
            "repo": self.repo, "sha": self.sha, "ecosystem": ecosystem,
            "source_file": "src/index.ts", "import_raw": "left-pad",
            "import_root": "left-pad", "declared_dependency_match": status,
            "declared_package": "left-pad", "version_spec": "^1.3.0",
            "resolved_version": "1.3.0", "resolution_source": "manifest",
            "dependency_group": "dependencies", "dep_file_path": "package.json",
            "dependency_file_changed_in_diff": "false",
        }

    def _python_dependency(self, sha, package, version):
        return {
            "repo": self.repo, "sha": sha, "repo_language": "Python",
            "author_date": "2026-07-01T00:00:00Z",
            "dep_file_path": "requirements.txt", "package_name": package,
            "version_spec": f"=={version}", "resolved_version": version,
            "resolution_source": "manifest", "version_kind": "exact",
            "dependency_group": "requirements", "namespace_hint": "",
            "dependency_file_changed_in_diff": "false",
        }

    def _go_dependency(self, package, version="", source="manifest", path="go.mod"):
        return {
            "repo": self.repo, "sha": self.sha, "repo_language": "Go",
            "author_date": "2026-07-01T00:00:00Z", "dep_file_path": path,
            "package_name": package, "version_spec": version,
            "resolved_version": version, "resolution_source": source,
            "version_kind": "unresolved" if source == "first_party_module" else "exact",
            "dependency_group": "module" if source == "first_party_module" else "require",
            "namespace_hint": "", "dependency_file_changed_in_diff": "false",
        }

    def _build_python_wheel_database(self):
        matches = self.root / "matches.csv"
        dependencies = self.root / "dependencies.csv"
        wheel_map = self.root / "wheel.csv"
        negative = self._legacy("not_observed_in_parsed_dependency_files", "PyPI")
        negative.update({"source_file": "src/app.py", "import_raw": "cv2.dnn", "import_root": "cv2", "declared_package": ""})
        self._write_csv(matches, [negative])
        self._write_csv(dependencies, [
            self._python_dependency("6" * 40, "opencv-python", "4.10.0.84"),
            self._python_dependency("7" * 40, "package-a", "1.0.0"),
            self._python_dependency("7" * 40, "package-b", "2.0.0"),
        ])
        self._write_csv(wheel_map, [
            self._wheel("opencv-python", "4.10.0.84", "cv2", "ok"),
            self._wheel("opencv_python", "4.10.0.84", "cv2", "ok"),
            self._wheel("package-a", "1.0.0", "shared", "ok"),
            self._wheel("package-b", "2.0.0", "shared", "ok"),
            self._wheel("missing-dist", "1.0.0", "", "no_wheel"),
        ])
        database = self.root / "python-context.sqlite"
        sources = [
            context.ContextSource("Python", matches),
            context.ContextSource("Python", dependencies, "python_dependencies"),
            context.ContextSource("Python", wheel_map, "python_wheel_map"),
        ]
        return database, context.build_legacy_context_database(database, sources)

    def _lookup_contexts(self, database, first_sha, second_sha):
        lookup = context.SqliteContextLookup(database)
        try:
            first = lookup.lookup({"repo": self.repo, "sha": first_sha})
            second = lookup.lookup({"repo": self.repo, "sha": second_sha})
            return first, second
        finally:
            lookup.close()

    def _python_event(self):
        event = self._event("import", "src/app.py", "cv2", "")
        event.update({
            "actual_language": "Python", "ecosystem": "PyPI",
            "raw_target": "cv2.dnn", "package_candidate": "cv2",
            "event_status": "direct_or_normalized_mapping",
        })
        return event

    def _go_event(self, target, path):
        event = self._event("import", path, target, "")
        event.update({
            "actual_language": "Go", "ecosystem": "Go",
            "raw_target": target, "package_candidate": target,
            "event_status": "go_module_path_mapping",
        })
        return event

    def _wheel_context_rows(self):
        negative = context.normalize_context({
            "repo": self.repo, "sha": self.sha, "source_path": "src/app.py",
            "raw_target": "cv2.dnn", "import_root": "cv2", "ecosystem": "PyPI",
            "alignment_status": "mapping_uncertain",
            "resolution_source": "legacy_no_match",
            "reason_codes": "mapping_uncertain|python_alias_not_found",
        })
        bridge = context.normalize_context({
            "repo": self.repo, "sha": self.sha, "source_path": "*",
            "raw_target": "cv2", "import_root": "cv2", "ecosystem": "PyPI",
            "package_name": "opencv-python", "resolved_version": "4.10.0.84",
            "alignment_status": "matched_existing_manifest",
            "resolution_source": "python_wheel_metadata",
            "reason_codes": "python_wheel_metadata",
        })
        return negative, bridge

    @staticmethod
    def _wheel(package, version, root, status):
        return {
            "package_name": package, "version": version, "import_root": root,
            "status": status, "source": "wheel_metadata" if status == "ok" else "",
            "wheel_filename": f"{package}-{version}.whl" if status == "ok" else "",
            "wheel_size": "1024" if status == "ok" else "", "error": "",
        }

    @staticmethod
    def _write_csv(path, rows):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0])
            writer.writeheader()
            writer.writerows(rows)

    def _commit(self, imports, manifests):
        return {
            "commit_id": self.commit_id, "repo": self.repo, "sha": self.sha,
            "author_date": "2026-07-01T00:00:00Z", "agent": "Codex",
            "repo_primary_language": "TypeScript",
            "actual_changed_languages": "TypeScript",
            "import_event_count": imports, "manifest_event_count": manifests,
            "dependency_quadrant": "I+M-" if manifests == "0" else "I+M+",
            "schema_version": "2",
        }

    def _event(self, kind, path, package, version, ordinal=0):
        event_id = schema.make_event_id(
            self.commit_id, kind, path, ordinal + 1, ordinal, package
        )
        return {
            "event_id": event_id, "commit_id": self.commit_id,
            "event_type": kind, "path": path, "actual_language": "TypeScript",
            "ecosystem": "npm", "new_line_number": str(ordinal + 1),
            "event_ordinal": str(ordinal), "raw_target": package,
            "package_candidate": package, "version_spec": version,
            "event_status": "direct_mapping" if kind == "import" else "direct_manifest",
            "reason_codes": "", "schema_version": "2",
        }

    def _context(self, status, resolved=""):
        return context.normalize_context({
            "repo": self.repo, "sha": self.sha, "source_path": "src/index.ts",
            "raw_target": "left-pad", "import_root": "left-pad",
            "ecosystem": "npm", "package_name": "left-pad",
            "version_spec": "^1.3.0", "resolved_version": resolved,
            "resolution_source": "manifest+lockfile",
            "dependency_group": "dependencies", "dependency_path": "package.json",
            "alignment_status": status, "reason_codes": "",
        })


if __name__ == "__main__":
    unittest.main()

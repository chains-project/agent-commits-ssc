import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_DIR))

import rq2_stage2_episode_linker as linker
import rq2_stage_schema as schema


def commit_row(import_count, manifest_count, quadrant="I+M+"):
    commit_id = schema.make_commit_id("owner/repo", "1" * 40)
    return {
        "commit_id": commit_id, "repo": "owner/repo", "sha": "1" * 40,
        "author_date": "2026-07-01T00:00:00Z", "agent": "Codex",
        "repo_primary_language": "TypeScript", "actual_changed_languages": "TypeScript",
        "import_event_count": str(import_count), "manifest_event_count": str(manifest_count),
        "dependency_quadrant": quadrant, "schema_version": "2",
    }


def event_row(commit_id, ordinal, event_type, package, version="", **values):
    path = values.get("path", "src/index.ts" if event_type == "import" else "package.json")
    raw = values.get("raw_target", package or "org.example.Widget")
    event_id = schema.make_event_id(commit_id, event_type, path, ordinal + 1, ordinal, raw)
    return {
        "event_id": event_id, "commit_id": commit_id, "event_type": event_type,
        "path": path, "actual_language": values.get("actual_language", "TypeScript"),
        "ecosystem": values.get("ecosystem", "npm"),
        "new_line_number": str(ordinal + 1), "event_ordinal": str(ordinal),
        "raw_target": raw, "package_candidate": package, "version_spec": version,
        "event_status": values.get("event_status", "direct_mapping" if event_type == "import" else "direct_manifest"),
        "reason_codes": values.get("reason_codes", ""), "schema_version": "2",
    }


class EpisodeLinkerTest(unittest.TestCase):
    def test_duplicate_imports_share_one_exact_episode(self):
        commit = commit_row(2, 1)
        events = [
            event_row(commit["commit_id"], 0, "import", "left-pad", path="src/a.ts"),
            event_row(commit["commit_id"], 1, "import", "left-pad", path="src/b.ts"),
            event_row(commit["commit_id"], 2, "manifest_addition", "left-pad", "1.3.0"),
        ]
        episodes, links = linker.build_episode_tables([commit], events)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(len(links), 3)
        self.assertEqual(episodes[0]["dependency_quadrant"], "I+M+")
        self.assertEqual(episodes[0]["query_kind"], "exact")
        self.assertEqual(episodes[0]["registry_status"], "not_queried")

    def test_manifest_mismatch_splits_two_episodes(self):
        commit = commit_row(1, 1)
        events = [
            event_row(commit["commit_id"], 0, "import", "axios"),
            event_row(commit["commit_id"], 1, "manifest_addition", "left-pad", "1.3.0"),
        ]
        episodes, _links = linker.build_episode_tables([commit], events)
        self.assertEqual({row["dependency_quadrant"] for row in episodes}, {"I+M-", "I-M+"})
        imported = next(row for row in episodes if row["dependency_quadrant"] == "I+M-")
        self.assertIn("manifest_does_not_explain_import", imported["reason_codes"])

    def test_private_local_dependency_is_excluded(self):
        commit = commit_row(1, 1)
        events = [
            event_row(commit["commit_id"], 0, "import", "local-package"),
            event_row(commit["commit_id"], 1, "manifest_addition", "local-package", "file:../local"),
        ]
        episodes, _links = linker.build_episode_tables([commit], events)
        self.assertEqual(episodes[0]["query_kind"], "excluded")
        self.assertEqual(episodes[0]["registry_status"], "excluded")
        self.assertEqual(episodes[0]["alignment_status"], "private_or_local_excluded")

    def test_java_unique_coordinate_and_ambiguous_mapping(self):
        unique_commit = commit_row(1, 1)
        java = event_row(
            unique_commit["commit_id"], 0, "import", "", ecosystem="Maven",
            path="src/main/java/App.java", actual_language="Java",
            raw_target="org.example.Widget", event_status="mapping_uncertain",
            reason_codes="mapping_uncertain",
        )
        manifest = event_row(
            unique_commit["commit_id"], 1, "manifest_addition", "org.example:core",
            "1.0.0", ecosystem="Maven", path="pom.xml", actual_language="Java",
        )
        episodes, links = linker.build_episode_tables([unique_commit], [java, manifest])
        self.assertEqual(len(episodes), 1)
        self.assertEqual(len(links), 2)
        self.assertEqual(episodes[0]["alignment_status"], "matched_manifest_unique_coordinate")

    def test_unresolved_import_remains_auditable(self):
        commit = commit_row(1, 0, "I+M-")
        java = event_row(
            commit["commit_id"], 0, "import", "", ecosystem="Maven",
            path="src/App.java", actual_language="Java", raw_target="org.example.Widget",
            event_status="mapping_uncertain", reason_codes="mapping_uncertain",
        )
        episodes, links = linker.build_episode_tables([commit], [java])
        self.assertEqual((len(episodes), len(links)), (1, 1))
        self.assertEqual(episodes[0]["query_kind"], "unresolved")
        self.assertEqual(episodes[0]["registry_status"], "cannot_compare")

    def test_unresolved_targets_with_outer_whitespace_share_episode(self):
        commit = commit_row(2, 0, "I+M-")
        events = [
            event_row(
                commit["commit_id"], 0, "import", "", raw_target=",",
                event_status="mapping_uncertain",
                reason_codes="invalid_npm_package_candidate",
            ),
            event_row(
                commit["commit_id"], 1, "import", "", raw_target=", ",
                event_status="mapping_uncertain",
                reason_codes="invalid_npm_package_candidate",
            ),
        ]

        episodes, links = linker.build_episode_tables([commit], events)

        self.assertEqual(len(episodes), 1)
        self.assertEqual(len(links), 2)
        self.assertEqual(episodes[0]["query_kind"], "unresolved")
        self.assertEqual(
            {link["event_id"] for link in links},
            {event["event_id"] for event in events},
        )

    def test_output_validation_rejects_unlinked_event(self):
        commit = commit_row(1, 0, "I+M-")
        event = event_row(commit["commit_id"], 0, "import", "left-pad")
        with self.assertRaisesRegex(ValueError, "not every Stage 1 event"):
            linker._validate_outputs([event], [], [])

    def test_unknown_commit_reference_is_rejected(self):
        commit = commit_row(0, 1, "I-M+")
        event = event_row("cmt_unknown", 0, "manifest_addition", "left-pad", "1.3.0")
        with self.assertRaisesRegex(ValueError, "unknown commit"):
            linker.build_episode_tables([commit], [event])


if __name__ == "__main__":
    unittest.main()

"""Validate the RQ2 synthetic benchmark v2 structure and scientific boundary.

[IN]: v2 scenarios JSON and v1-to-v2 migration JSON.
[OUT]: unittest assertions; no data files are generated.
[POS]: Oracle integrity gate; does not invoke Stage 1-3 or the network.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
V2_ROOT = ROOT / "tests" / "fixtures" / "rq2_synthetic_pipeline_v2"
V2_PATH = V2_ROOT / "scenarios.json"
MIGRATION_PATH = V2_ROOT / "migration.json"

TARGET_LANGUAGES = {
    "TypeScript",
    "Python",
    "JavaScript",
    "Rust",
    "Go",
    "Java",
}
QUERY_KINDS = {"exact", "name_only", "range", "unresolved", "excluded"}
REGISTRY_STATUSES = {
    "exists_before_author_date",
    "published_after_author_date",
    "current_absent_unknown",
    "no_matching_version_before_author_date",
    "cannot_compare",
    "excluded",
}
ADVISORY_STATUSES = {
    "active_before_or_at_author_date",
    "published_after_author_date",
    "withdrawn_before_author_date",
    "hydration_incomplete",
    "none_observed",
    "not_queried",
}
REQUIRED_NEW_IDS = {
    "tsx_extension_overrides_index_hint",
    "multilang_python_in_typescript_repo",
    "multilang_java_in_python_repo",
    "multiple_imports_single_line_javascript",
    "manifest_only_python_exact",
    "manifest_only_rust_exact",
    "manifest_only_go_exact",
    "manifest_only_java_exact",
    "javascript_name_only_exists",
    "range_no_match",
    "unsupported_range_syntax",
    "java_maven_property_resolved",
    "java_maven_property_unresolved",
    "java_gradle_catalog_resolved",
    "java_mapping_one_candidate",
    "java_mapping_multiple_candidates",
    "python_alias_mapping_failed",
    "osv_withdrawn_before_commit",
    "osv_hydration_failed",
    "duplicate_events_single_episode",
    "javascript_six_family_matrix",
    "python_six_family_matrix",
    "rust_six_family_matrix",
    "go_six_family_matrix",
    "java_entry_control",
    "java_registry_absence",
    "java_gradle_local_project_control",
}
MATRIX_FAMILIES = {f"C{index}" for index in range(1, 9)}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class SyntheticOracleV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.v2 = read_json(V2_PATH)
        cls.migration = read_json(MIGRATION_PATH)
        cls.v1_ids = [item["id"] for item in cls.migration["items"]]
        cls.v2_scenarios = cls.v2["scenarios"]
        cls.v2_ids = [item["id"] for item in cls.v2_scenarios]

    def test_declared_count_and_ids_are_unique(self) -> None:
        self.assertEqual(self.v2["scenario_count"], 49)
        self.assertEqual(len(self.v2_scenarios), 49)
        self.assertEqual(len(set(self.v2_ids)), len(self.v2_ids))

    def test_all_v1_ids_are_retained_in_original_order(self) -> None:
        self.assertEqual(len(self.v1_ids), 22)
        self.assertEqual(self.v2_ids[: len(self.v1_ids)], self.v1_ids)
        retained = [row["id"] for row in self.v2_scenarios if row["origin"] == "retained_v1"]
        self.assertEqual(retained, self.v1_ids)

    def test_migration_is_total_and_new_ids_are_exact(self) -> None:
        migrated = [item["id"] for item in self.migration["items"]]
        self.assertEqual(migrated, self.v1_ids)
        self.assertEqual(self.migration["old_scenario_count"], 22)
        self.assertEqual(self.migration["new_scenario_count"], 49)
        self.assertEqual(set(self.migration["new_ids"]), REQUIRED_NEW_IDS)
        self.assertEqual(set(self.v2_ids) - set(self.v1_ids), REQUIRED_NEW_IDS)

    def test_target_languages_are_frozen_and_covered(self) -> None:
        self.assertEqual(set(self.v2["target_languages"]), TARGET_LANGUAGES)
        observed = {item["language"] for item in self.v2_scenarios}
        self.assertEqual(observed, TARGET_LANGUAGES)

    def test_every_scenario_has_english_summary(self) -> None:
        for scenario in self.v2_scenarios:
            with self.subTest(scenario=scenario["id"]):
                self.assertTrue(scenario["input_summary"].strip())

    def test_capture_points_are_explicit_and_resolvable(self) -> None:
        definitions = self.v2["capture_point_definitions"]
        self.assertTrue(definitions)
        for scenario in self.v2_scenarios:
            with self.subTest(scenario=scenario["id"]):
                references = scenario.get(
                    "capture_points", self.v2["default_capture_points"]
                )
                self.assertTrue(references)
                self.assertTrue(set(references) <= set(definitions))
        for point in definitions.values():
            self.assertTrue(point["stage"])
            self.assertTrue(point["file"])
            self.assertTrue(point["functions"])

    def test_six_language_common_family_matrix_is_total(self) -> None:
        matrix = self.v2["coverage_matrix"]
        self.assertEqual(set(matrix), TARGET_LANGUAGES)
        for language, families in matrix.items():
            with self.subTest(language=language):
                self.assertEqual(set(families), MATRIX_FAMILIES)
                for scenario_ids in families.values():
                    self.assertTrue(scenario_ids)
                    for scenario_id in scenario_ids:
                        self.assertEqual(self.by_id(scenario_id)["language"], language)

    def test_stage1_and_episode_schema_is_complete(self) -> None:
        for scenario in self.v2_scenarios:
            with self.subTest(scenario=scenario["id"]):
                expected = scenario["expected"]
                stage1 = expected["stage1"]
                self.assertIn("candidate", stage1)
                self.assertIn("import_events", stage1)
                self.assertIn("manifest_events", stage1)
                episodes = expected["episodes"]
                if stage1["candidate"]:
                    self.assertGreaterEqual(len(episodes), 1)
                else:
                    self.assertEqual(episodes, [])
                for episode in episodes:
                    self.assertIn(episode["quadrant"], {"I+M+", "I+M-", "I-M+"})
                    self.assertTrue(episode["alignment_status"])
                    self.assertIn(episode["query"]["kind"], QUERY_KINDS)
                    self.assertIn(episode["registry_status"], REGISTRY_STATUSES)
                    self.assertIn(episode["advisory_status"], ADVISORY_STATUSES)
                    self.assertIsInstance(episode["reasons"], list)

    def test_manifest_only_is_reachable(self) -> None:
        scenario = self.by_id("i0_m1_manifest_only")
        self.assertTrue(scenario["expected"]["stage1"]["candidate"])
        self.assertEqual(scenario["expected"]["stage1"]["import_events"], 0)
        self.assertEqual(scenario["expected"]["stage1"]["manifest_events"], 1)
        self.assertEqual(scenario["expected"]["episodes"][0]["quadrant"], "I-M+")

    def test_alignment_and_registry_are_independent(self) -> None:
        episodes = self.by_id("manifest_mismatch")["expected"]["episodes"]
        self.assertEqual({row["quadrant"] for row in episodes}, {"I+M-", "I-M+"})
        self.assertTrue(all(row["registry_status"] == "exists_before_author_date" for row in episodes))

    def test_current_absence_without_history_is_weak(self) -> None:
        for scenario in self.v2_scenarios:
            for episode in scenario["expected"]["episodes"]:
                no_history = episode["advisory_status"] == "none_observed"
                if episode["registry_status"] == "current_absent_unknown" and no_history:
                    self.assertIn("unknown_deleted_or_unpublished_possible", episode["reasons"])

    def test_multilanguage_cases_ignore_repo_primary_language(self) -> None:
        for scenario_id in {
            "multilang_python_in_typescript_repo",
            "multilang_java_in_python_repo",
        }:
            scenario = self.by_id(scenario_id)
            self.assertNotEqual(scenario["language"], scenario["repo_primary_language"])
            self.assertEqual(
                scenario["expected"]["stage1"]["detected_languages"],
                [scenario["language"]],
            )

    def test_java_failures_remain_in_oracle(self) -> None:
        unresolved = self.by_id("java_maven_property_unresolved")["expected"]["episodes"][0]
        ambiguous = self.by_id("java_mapping_multiple_candidates")["expected"]["episodes"][0]
        self.assertIn("version_unresolved", unresolved["reasons"])
        self.assertIn("mapping_uncertain", ambiguous["reasons"])
        self.assertEqual(unresolved["query"]["kind"], "name_only")
        self.assertEqual(ambiguous["query"]["kind"], "unresolved")

    def test_advisory_axes_do_not_erase_registry_evidence(self) -> None:
        hydrated = self.by_id("osv_hydration_failed")["expected"]["episodes"][0]
        withdrawn = self.by_id("osv_withdrawn_before_commit")["expected"]["episodes"][0]
        self.assertEqual(hydrated["registry_status"], "exists_before_author_date")
        self.assertEqual(hydrated["advisory_status"], "hydration_incomplete")
        self.assertEqual(withdrawn["advisory_status"], "withdrawn_before_author_date")

    def test_duplicate_events_share_one_episode(self) -> None:
        scenario = self.by_id("duplicate_events_single_episode")
        stage1 = scenario["expected"]["stage1"]
        episode = scenario["expected"]["episodes"][0]
        self.assertEqual(stage1["import_events"], 2)
        self.assertEqual(episode["linked_import_events"], 2)
        self.assertEqual(episode["registry_query_count"], 1)

    def test_live_cases_do_not_embed_manufactured_evidence(self) -> None:
        for scenario in self.v2_scenarios:
            if scenario["execution_mode"] == "live_integration":
                self.assertNotIn("evidence_fixture", scenario["input"])

    def by_id(self, scenario_id: str) -> dict[str, Any]:
        return next(item for item in self.v2_scenarios if item["id"] == scenario_id)


if __name__ == "__main__":
    unittest.main()

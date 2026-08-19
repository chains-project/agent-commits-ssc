import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "tests" / "fixtures" / "rq2_synthetic_pipeline_v2" / "output_schema_v2.json"


class OutputSchemaV2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_exactly_four_canonical_tables(self):
        expected = {"commits.csv", "events.csv", "episodes.csv", "episode_event_links.csv"}
        self.assertEqual(expected, set(self.schema["tables"]))

    def test_primary_keys_are_declared_fields(self):
        for table_name, table in self.schema["tables"].items():
            with self.subTest(table=table_name):
                self.assertTrue(table["primary_key"])
                self.assertTrue(set(table["primary_key"]).issubset(table["fields"]))

    def test_fields_are_unique_and_versioned(self):
        for table_name, table in self.schema["tables"].items():
            with self.subTest(table=table_name):
                self.assertEqual(len(table["fields"]), len(set(table["fields"])))
                self.assertIn("schema_version", table["fields"])

    def test_registry_and_advisory_axes_are_separate(self):
        fields = self.schema["tables"]["episodes.csv"]["fields"]
        self.assertIn("registry_status", fields)
        self.assertIn("advisory_status", fields)

    def test_repeated_commit_metadata_is_normalized(self):
        repeated = {"repo", "sha", "author_date", "agent"}
        self.assertTrue(repeated.issubset(self.schema["tables"]["commits.csv"]["fields"]))
        for table_name in ("events.csv", "episodes.csv", "episode_event_links.csv"):
            with self.subTest(table=table_name):
                self.assertTrue(repeated.isdisjoint(self.schema["tables"][table_name]["fields"]))

    def test_detailed_outputs_are_debug_only(self):
        debug_only = set(self.schema["debug_only_outputs"])
        self.assertIn("parsed_dependencies.csv", debug_only)
        self.assertTrue(debug_only.isdisjoint(self.schema["tables"]))


if __name__ == "__main__":
    unittest.main()

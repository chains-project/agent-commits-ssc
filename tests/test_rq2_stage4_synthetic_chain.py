import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "tests"
sys.path.insert(0, str(TEST_DIR))

import run_rq2_stage4_synthetic_chain as stage4


class Stage4SyntheticChainTest(unittest.TestCase):
    def test_all_scenarios_run_without_unclassified_regression(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result"
            summary = stage4.run_validation(
                stage4.FIXTURE_DIR / "scenarios.json",
                stage4.FIXTURE_DIR / "migration.json",
                output,
            )
            self.assertEqual(summary["scenario_count"], 49)
            self.assertEqual(summary["v1_retained_count"], 22)
            self.assertEqual(summary["status_counts"].get("unexpected_regression", 0), 0)
            self.assertEqual(summary["status_counts"].get("known_implementation_gap", 0), 0)
            self.assertEqual(summary["status_counts"].get("out_of_scope_limitation", 0), 0)
            self.assertEqual(summary["in_scope_count"], 49)
            self.assertEqual(summary["in_scope_pass_count"], 49)
            self.assertEqual(summary["canonical_csv_persisted"], 0)
            for result in summary["results"]:
                self.assertIn("input_summary", result)
                self.assertTrue(result["capture_points"])
                self.assertIn("stage1", result["expected"])
                self.assertIn("stage1", result["actual"])
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {"stage4_summary.json"},
            )

    def test_output_is_refused_when_non_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result"
            output.mkdir()
            (output / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                stage4.run_validation(
                    stage4.FIXTURE_DIR / "scenarios.json",
                    stage4.FIXTURE_DIR / "migration.json",
                    output,
                )


if __name__ == "__main__":
    unittest.main()

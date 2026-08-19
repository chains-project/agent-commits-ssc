"""Tests for the complete RQ2 adjudication synthetic chain."""

from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


import run_rq2_adjudication_synthetic_chain as chain


class AdjudicationSyntheticChainTests(unittest.TestCase):
    def test_all_scenarios_reach_expected_final_adjudication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result"
            summary = chain.run_validation(
                chain.FIXTURE_DIR / "scenarios.json",
                output,
            )
            self.assertEqual(summary["scenario_count"], 9)
            self.assertEqual(summary["status_counts"], {"pass": 9})
            self.assertTrue(summary["all_target_languages_covered"])
            self.assertTrue(summary["all_target_labels_covered"])
            self.assertEqual(summary["canonical_csv_persisted"], 0)
            self.assertEqual(
                summary["label_counts"],
                {
                    "confirmed_hallucination": 2,
                    "indeterminate": 1,
                    "not_hallucination": 4,
                    "probable_hallucination": 2,
                },
            )
            for result in summary["results"]:
                self.assertEqual(len(result["capture_points"]), 5)
                self.assertEqual(result["actual"]["commented_manifest_corrections"], 0)
                self.assertEqual(result["status"], "pass")
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {"summary.json"},
            )

    def test_output_is_refused_when_non_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result"
            output.mkdir()
            (output / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                chain.run_validation(
                    chain.FIXTURE_DIR / "scenarios.json",
                    output,
                )


if __name__ == "__main__":
    unittest.main()

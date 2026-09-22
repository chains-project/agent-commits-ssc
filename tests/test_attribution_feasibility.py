"""Check descriptive replay and rejection of inconsistent frozen inputs."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "scripts/exploration/model_observability"
sys.path.insert(0, str(MODULE_DIR))
from summarize_attribution_feasibility import load_ranked, summarize, verify


class AttributionFeasibilityTests(unittest.TestCase):
    def setUp(self):
        data = ROOT / "data/model_observability"
        self.sample = load_ranked(data / "EXPLORATORY_PROBABILITY_SAMPLE.csv")
        self.codes = load_ranked(data / "EXPLORATORY_AGENT_CONSENSUS.csv")

    def test_published_counts_match(self):
        result = verify(ROOT)["sample_counts"]
        self.assertEqual(result["labels"], {"X0": 72, "X2": 8, "X3": 34, "XC": 6})
        self.assertEqual(result["positive_rows"], 42)
        self.assertEqual(result["positives_supported_only_by_message_or_trailer"], 42)
        self.assertEqual(result["positives_citing_other_sources"], 0)
        self.assertEqual(result["diff_index_matches"], 116)
        self.assertEqual(result["readable_diffs"], 109)

    def test_missing_case_rejected(self):
        self.codes.pop(120)
        with self.assertRaisesRegex(ValueError, "ranks"):
            summarize(self.sample, self.codes)

    def test_unreadable_diff_cannot_be_counted_as_available(self):
        row = next(r for r in self.sample.values() if r["diff_status"] == "error")
        row["diff_available"] = "true"
        with self.assertRaisesRegex(ValueError, "availability"):
            summarize(self.sample, self.codes)

    def test_disagreement_cannot_be_promoted(self):
        row = next(r for r in self.codes.values() if r["consensus_rule"] == "disagreement_to_XC")
        row["x_level"] = "X3"
        with self.assertRaisesRegex(ValueError, "disagreement"):
            summarize(self.sample, self.codes)

    def test_additional_source_is_reported(self):
        codes = copy.deepcopy(self.codes)
        row = next(r for r in codes.values() if r["x_level"] == "X3")
        row["source_type"] = "commit_trailer;same_commit_session_artifact"
        result = summarize(self.sample, codes)
        self.assertEqual(result["positives_citing_other_sources"], 1)
        self.assertEqual(result["positives_supported_only_by_message_or_trailer"], 41)


if __name__ == "__main__":
    unittest.main()

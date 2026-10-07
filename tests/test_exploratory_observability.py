"""Small deterministic tests for E0-E3 exploratory observability helpers."""

import csv
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
import sys
from unittest.mock import mock_open, patch

MODULE_DIR = Path(__file__).resolve().parents[1] / "scripts" / "exploration" / "model_observability"
sys.path.insert(0, str(MODULE_DIR))

from build_exploratory_probability_sample import select_rows
from exploratory_observability_common import classify_message, compile_lexicon, redact
from extract_exploratory_diff_context import artifact_context
from finalize_exploratory_agent_coding import read_codes

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lexicon = json.loads((MODULE_DIR / "EXPLORATORY_MODEL_PROXY_LEXICON.v1.json").read_text(encoding="utf-8"))
        cls.compiled = compile_lexicon(cls.lexicon)

    def test_unique_ids_and_regexes(self):
        ids = [row["signal_id"] for row in self.lexicon["patterns"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(self.compiled[0])

    def test_direct_exact(self):
        x = classify_message("Implemented by Claude Sonnet 4.5 for this commit", self.compiled)
        self.assertEqual(x["sampling_stratum"], "L3")
        self.assertEqual(x["granularity"], "exact_model")

    def test_structured_exact(self):
        x = classify_message("model: qwen3-coder", self.compiled)
        self.assertEqual(x["sampling_stratum"], "L2")
        self.assertEqual(x["granularity"], "exact_model")

    def test_mention_and_none(self):
        self.assertEqual(classify_message("docs: benchmark DeepSeek-V3", self.compiled)["sampling_stratum"], "L1")
        self.assertEqual(classify_message("fix parser whitespace", self.compiled)["sampling_stratum"], "L0")

    def test_redaction(self):
        value = redact("https://x.test/a a@b.test @login deadbeefdeadbeef")
        self.assertNotIn("x.test", value); self.assertNotIn("a@b", value); self.assertNotIn("@login", value); self.assertNotIn("deadbeef", value)

    def test_sampling_cap_and_determinism(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame.csv"
            fields = ["repo_sha_key", "repo_key", "developer_key", "sampling_stratum"]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                for s in ("L3", "L2", "L1", "L0"):
                    writer.writerows({"repo_sha_key": f"{s}-{i}", "repo_key": f"r-{i}", "developer_key": f"d-{i}", "sampling_stratum": s} for i in range(50))
            c1, a = select_rows(path, "seed"); c2, b = select_rows(path, "seed")
            self.assertEqual([r["repo_sha_key"] for r in a], [r["repo_sha_key"] for r in b])
            self.assertEqual(len(a), 120); self.assertEqual(c1, c2)
            self.assertTrue(all(abs(float(r.get("within_cluster_N", 1)) - 1) < 1e-12 for r in a))

    def test_duplicate_coding_rank_rejected(self):
        with patch.object(Path, "open", return_value=io.StringIO("selection_rank,x_level\n1,X0\n1,X3\n")):
            with self.assertRaisesRegex(ValueError, "duplicate selection_rank"):
                read_codes(Path("codes.csv"), {1})

    def test_missing_coding_rank_rejected(self):
        with patch.object(Path, "open", return_value=io.StringIO("selection_rank,x_level\n1,X0\n")):
            with self.assertRaisesRegex(ValueError, "expected ranks"):
                read_codes(Path("codes.csv"), {1, 2})

    def test_unique_coding_rows_preserved(self):
        with patch.object(Path, "open", return_value=io.StringIO("selection_rank,x_level\n1,X0\n2,X3\n")):
            rows = read_codes(Path("codes.csv"), {1, 2})
        self.assertEqual([rows[k]["x_level"] for k in (1, 2)], ["X0", "X3"])

    def test_indexed_ok_requires_both_artifacts(self):
        index = dict(status="ok", files_count="1", json_path="commit.json", patch_path="commit.patch")
        for present in ((False, False), (False, True), (True, False)):
            with self.subTest(present=present), patch.object(Path, "is_file", side_effect=present):
                with self.assertRaisesRegex(FileNotFoundError, "Indexed-ok artifacts missing"):
                    artifact_context(index)

    def test_available_artifacts_retain_context(self):
        index = dict(status="ok", files_count="1", json_path="commit.json", patch_path="commit.patch")
        payload = json.dumps({"files": [{"filename": ".agent/config.json"}]})
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "read_text", return_value=payload):
            with patch.object(Path, "open", mock_open(read_data="model: gpt-5\n")):
                result = artifact_context(index)
        self.assertEqual(result["diff_available"], "true")
        self.assertEqual(result["config_candidate_files"], ".agent/config.json")
        self.assertEqual(result["diff_model_context"], "model: gpt-5")

    def test_unreadable_indexed_artifact_fails(self):
        index = dict(status="ok", files_count="1", json_path="commit.json", patch_path="commit.patch")
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "read_text", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                artifact_context(index)

    def test_unavailable_index_status_preserved(self):
        index = dict(status="error", files_count="0", json_path="commit.json", patch_path="commit.patch")
        with patch.object(Path, "is_file", return_value=False):
            result = artifact_context(index)
        self.assertEqual(result["diff_available"], "false")
        self.assertEqual(result["diff_status"], "error")
        self.assertEqual(artifact_context(None)["diff_status"], "not_in_local_index")

    def test_sparse_cluster_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame.csv"
            fields = ["repo_sha_key", "repo_key", "developer_key", "sampling_stratum"]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                for s in ("L3", "L1", "L0"):
                    writer.writerows({"repo_sha_key": f"{s}-{i}", "repo_key": f"r-{i}", "developer_key": f"d-{i}", "sampling_stratum": s} for i in range(30))
                for dev, size in (("a", 34), ("b", 7), ("c", 2)):
                    writer.writerows({"repo_sha_key": f"L2-{dev}-{i}", "repo_key": f"r-{dev}-{i}", "developer_key": dev, "sampling_stratum": "L2"} for i in range(size))
                writer.writerows({"repo_sha_key": f"L2-u-{i}", "repo_key": f"ru-{i}", "developer_key": f"u-{i}", "sampling_stratum": "L2"} for i in range(13))
            _, rows = select_rows(path, "seed")
            l2 = [r for r in rows if r["sampling_stratum"] == "L2"]
            self.assertEqual(len(l2), 30)
            self.assertLessEqual(max(Counter(r["developer_key"] for r in l2).values()), 8)

if __name__ == "__main__": unittest.main()

"""Tests for the path-neutral public RQ2 analysis entry points."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


summary = load_module(
    "summarize_final_adjudication",
    "scripts/rq2/analysis/summarize_final_adjudication.py",
)
verify = load_module(
    "verify_published_product",
    "scripts/rq2/analysis/verify_published_product.py",
)


class PublicAnalysisTests(unittest.TestCase):
    def test_summary_counts_minimal_rows(self) -> None:
        rows = [
            {
                "repo": "owner/repo", "sha": "a", "agent": "agent-a",
                "ecosystem": "npm", "languages_json": '["TypeScript"]',
                "final_label": "confirmed_hallucination", "confidence": "high",
                "patch_path": "diff_corpus/TypeScript/owner__repo/a.patch",
            },
            {
                "repo": "owner/repo", "sha": "a", "agent": "agent-a",
                "ecosystem": "npm", "languages_json": '["TypeScript"]',
                "final_label": "not_hallucination", "confidence": "high",
                "patch_path": "diff_corpus/TypeScript/owner__repo/a.patch",
            },
        ]
        result = summary.summarize(rows)
        self.assertEqual(result["adjudication_rows"], 2)
        self.assertEqual(result["unique_commits"], 1)
        self.assertTrue(result["portable_patch_paths"])

    def test_published_product_verifies(self) -> None:
        report = verify.verify(ROOT)
        self.assertEqual(report["status"], "pass", report["failed_checks"])
        self.assertEqual(report["check_count"], 12)


if __name__ == "__main__":
    unittest.main()

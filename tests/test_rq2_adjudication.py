"""Regression tests for the versionless RQ2 adjudication modules."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "scripts" / "rq2" / "adjudication"
sys.path.insert(0, str(MODULE_DIR))

import build_final_adjudication as adjudication  # noqa: E402
import apply_commented_manifest_corrections as comments  # noqa: E402


def cargo_case() -> dict[str, str]:
    return {"ecosystem": "Cargo", "package_name": "candle"}


class SemanticPatchEvidenceTests(unittest.TestCase):
    def test_relevant_patch_index_is_memory_bounded(self) -> None:
        self.assertEqual(adjudication.relevant_patch_lines.cache_info().maxsize, 32)

    def test_source_model_path_does_not_mark_dependency_nonpublic(self) -> None:
        patch = """diff --git a/Cargo.toml b/Cargo.toml
+candle = { version = "0.9", optional = true }
diff --git a/src/candle.rs b/src/candle.rs
+let candle_model_path = config.path;
"""
        flags = adjudication.semantic_patch_flags(cargo_case(), patch)
        self.assertNotIn("cargo_nonpublic_spec", flags)

    def test_target_cargo_path_is_nonpublic(self) -> None:
        patch = """diff --git a/Cargo.toml b/Cargo.toml
+candle = { version = "0.9", path = "../candle" }
"""
        flags = adjudication.semantic_patch_flags(cargo_case(), patch)
        self.assertIn("cargo_nonpublic_spec", flags)

    def test_other_dependency_path_does_not_contaminate_target(self) -> None:
        patch = """diff --git a/Cargo.toml b/Cargo.toml
+peimon-inference = { path = "crates/inference", features = ["candle"] }
+candle = { version = "0.9", optional = true }
"""
        flags = adjudication.semantic_patch_flags(cargo_case(), patch)
        self.assertNotIn("cargo_nonpublic_spec", flags)

    def test_unrelated_documentation_checksum_is_ignored(self) -> None:
        case = {"ecosystem": "Go", "package_name": "example.com/pkg"}
        patch = """diff --git a/README.md b/README.md
+example.com/pkg h1:fake
"""
        self.assertNotIn(
            "lock_integrity", adjudication.semantic_patch_flags(case, patch)
        )

    def test_npm_url_spec_is_nonregistry(self) -> None:
        case = {"ecosystem": "npm", "package_name": "example-package"}
        patch = """diff --git a/package.json b/package.json
+  "example-package": "https://example.test/example.tgz",
"""
        flags = adjudication.semantic_patch_flags(case, patch)
        self.assertIn("workspace_or_nonregistry_spec", flags)

    def test_pypi_direct_url_is_nonregistry(self) -> None:
        case = {"ecosystem": "PyPI", "package_name": "actiancortex"}
        patch = """diff --git a/requirements.txt b/requirements.txt
+actiancortex @ https://example.test/actiancortex.whl
"""
        flags = adjudication.semantic_patch_flags(case, patch)
        self.assertIn("workspace_or_nonregistry_spec", flags)

    def test_unrelated_url_in_docs_is_ignored(self) -> None:
        case = {"ecosystem": "PyPI", "package_name": "requests"}
        patch = """diff --git a/requirements.txt b/requirements.txt
+requests>=2
diff --git a/README.md b/README.md
+requests @ https://example.test/requests.whl
"""
        flags = adjudication.semantic_patch_flags(case, patch)
        self.assertNotIn("workspace_or_nonregistry_spec", flags)


class CommentedManifestCorrectionTests(unittest.TestCase):
    def test_comment_only_candidate_is_not_hallucination(self) -> None:
        source = {
            "final_label": "confirmed_hallucination",
            "confidence": "high",
            "adjudication_basis": "old",
            "evidence_flags_json": "[]",
        }
        row = comments.corrected_row(
            source, {"commented_lines_json": "[]"}
        )
        self.assertEqual(row["final_label"], "not_hallucination")
        self.assertIn(
            "commented_manifest_parser_false_positive",
            row["evidence_flags_json"],
        )


if __name__ == "__main__":
    unittest.main()

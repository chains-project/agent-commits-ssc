"""Regression tests for PEP 508 environment-marker normalization."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_ROOT))

import rq2_stage1_manifest_diff as stage1  # noqa: E402
import rq2_stage2_alignment as stage2  # noqa: E402


class RequirementMarkerTest(unittest.TestCase):
    def test_version_marker_is_not_part_of_registry_specifier(self):
        line = 'asyncio-compat>=0.1; python_version<"3.11"'
        self.assertEqual(stage1._requirement_parts(line), ("asyncio-compat", ">=0.1"))
        self.assertEqual(
            stage2.classify_version("PyPI", '>=0.1; python_version<"3.11"'),
            ("range", ">=0.1", ()),
        )

    def test_marker_only_requirement_becomes_name_only(self):
        line = 'importlib-metadata; python_version<"3.8"'
        self.assertEqual(stage1._requirement_parts(line), ("importlib-metadata", ""))
        self.assertEqual(
            stage2.classify_version("PyPI", '; python_version<"3.8"'),
            ("name_only", "", ("version_unresolved",)),
        )

    def test_direct_reference_keeps_url_but_drops_marker(self):
        line = 'demo @ https://example.test/pkg.whl; python_version>="3.10"'
        expected = ("demo", "@ https://example.test/pkg.whl")
        self.assertEqual(stage1._requirement_parts(line), expected)


if __name__ == "__main__":
    unittest.main()

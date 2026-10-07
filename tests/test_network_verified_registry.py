from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[1] / "scripts" / "exploration" / "model_observability"
sys.path.insert(0, str(MODULE_DIR))


ROOT = MODULE_DIR


class QwenNetworkRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads((ROOT / "SIGNATURE_REGISTRY.v1.1.0.json").read_text(encoding="utf-8"))
        cls.pattern = next(row for row in cls.registry["patterns"] if row["signal_id"] == "h1_qwen_code_coauthor_trailer")
        cls.regex = re.compile(cls.pattern["pattern"])

    def test_version_and_no_execution_claim(self) -> None:
        self.assertEqual(self.registry["registry_version"], "harness-model-attribution-v1.1.0")
        self.assertEqual(self.registry["binding_rules"], [])
        self.assertFalse(any(row["evidence_tier"] == "M2" for row in self.registry["patterns"]))

    def test_exact_official_trailer_matches(self) -> None:
        self.assertIsNotNone(self.regex.search("Co-authored-by: Qwen-Coder <qwen-coder@alibabacloud.com>"))
        self.assertIsNotNone(self.regex.search("co-authored-by: qwen-coder <QWEN-CODER@ALIBABACLOUD.COM>"))

    def test_name_only_and_wrong_email_do_not_match(self) -> None:
        self.assertIsNone(self.regex.search("Qwen Code"))
        self.assertIsNone(self.regex.search("Co-authored-by: Qwen-Coder <human@example.com>"))
        self.assertIsNone(self.regex.search("Quoted: Co-authored-by: Qwen-Coder <qwen-coder@alibabacloud.com>"))


if __name__ == "__main__":
    unittest.main()

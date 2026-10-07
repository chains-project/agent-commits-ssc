from __future__ import annotations

import json
import re
import sys
import unittest
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "scripts" / "exploration" / "model_observability"
sys.path.insert(0, str(ROOT))
import offline_signal_scanner as scanner  # noqa: E402


class RegistryPatternTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads((ROOT / "SIGNATURE_REGISTRY.json").read_text(encoding="utf-8"))
        cases = json.loads((Path(__file__).resolve().parent / "fixtures" / "model_observability" / "signature_cases.json").read_text(encoding="utf-8"))
        cls.patterns = {row["signal_id"]: row for row in cls.registry["patterns"]}
        cls.cases = {row["signal_id"]: row for row in cases}

    def matches(self, signal_id: str, text: str) -> bool:
        pattern = self.patterns[signal_id]
        regex = re.compile(pattern["pattern"]) if pattern["pattern_type"] == "regex" else None
        return bool(list(scanner.match_values(pattern, regex, text)))

    def test_fixture_covers_every_pattern(self) -> None:
        self.assertEqual(set(self.patterns), set(self.cases))

    def test_positive_fixture_for_every_pattern(self) -> None:
        for signal_id, case in self.cases.items():
            with self.subTest(signal_id=signal_id):
                self.assertTrue(self.matches(signal_id, case["positive"]))

    def test_negative_and_exclusion_fixture_for_every_pattern(self) -> None:
        for signal_id, case in self.cases.items():
            with self.subTest(signal_id=signal_id):
                self.assertFalse(self.matches(signal_id, case["negative"]))
                self.assertFalse(self.matches(signal_id, case["excluded"]))

    def test_case_behavior_for_every_pattern(self) -> None:
        for signal_id, case in self.cases.items():
            with self.subTest(signal_id=signal_id):
                self.assertTrue(self.matches(signal_id, case["positive"].swapcase()))

    def test_ascii_boundary_for_every_pattern(self) -> None:
        for signal_id, case in self.cases.items():
            with self.subTest(signal_id=signal_id):
                wrapped = "X" + case["positive"] + "Y"
                self.assertFalse(self.matches(signal_id, wrapped))

    def test_unicode_boundary_for_every_pattern(self) -> None:
        for signal_id, case in self.cases.items():
            with self.subTest(signal_id=signal_id):
                positive = case["positive"]
                fullwidth_first = chr(ord(positive[0]) + 0xFEE0)
                confusable = fullwidth_first + positive[1:]
                self.assertFalse(self.matches(signal_id, confusable))

    def test_applicable_field_for_every_pattern(self) -> None:
        for signal_id, case in self.cases.items():
            pattern = self.patterns[signal_id]
            with self.subTest(signal_id=signal_id):
                self.assertIn(case["field"], pattern["applicable_fields"])


class ScannerHelperTests(unittest.TestCase):
    def test_full_message_and_trailer_extraction(self) -> None:
        envelope = {"item": {"commit": {"message": "Subject\n\nCo-authored-by: Codex <noreply@openai.com>", "author": {"email": "a@example.com", "date": "2026-01-02T00:00:00Z"}, "committer": {"email": "c@example.com"}}, "author": {"login": "a"}, "committer": {"login": "c"}}}
        fields, trailers, month = scanner.extract_fields(envelope)
        self.assertEqual(fields["message_first_line"], "Subject")
        self.assertEqual(trailers, ["Co-authored-by: Codex <noreply@openai.com>"])
        self.assertEqual(month, "2026-01")

    def test_redaction_removes_email_and_url(self) -> None:
        value = scanner.redact_span("By X <x@example.com> https://example.com/a", "trailer_line")
        self.assertNotIn("example.com", value)
        self.assertIn("<email>", value)
        self.assertIn("<url>", value)

    def test_no_model_binding_rules_exist(self) -> None:
        registry = json.loads((ROOT / "SIGNATURE_REGISTRY.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["binding_rules"], [])
        self.assertFalse(any(row["evidence_tier"] == "M2" for row in registry["patterns"]))

    def test_nul_physical_row_is_excluded_not_cleaned(self) -> None:
        content = "tier,agent,repo_sha\nmain,codex,repo|sha\n\x00bad,codex,bad|sha\n"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "rows.csv"
            path.write_text(content, encoding="utf-8")
            states, invalid = scanner.load_population(path, "supplementary_4h")
        self.assertEqual(set(states), {"repo|sha"})
        self.assertEqual(invalid, 1)

    def test_empty_message_field_is_available_no_match(self) -> None:
        fields = {"full_commit_message": "", "message_first_line": "", "author_email": "", "committer_email": "", "author_login": "", "committer_login": ""}
        bits = scanner.availability_bits(fields)
        self.assertTrue(bits & scanner.RAW_RECORD_BIT)
        self.assertTrue(bits & scanner.FIELD_BITS["full_commit_message"])
        self.assertTrue(bits & scanner.FIELD_BITS["message_first_line"])
        self.assertTrue(bits & scanner.FIELD_BITS["trailer_line"])


if __name__ == "__main__":
    unittest.main()

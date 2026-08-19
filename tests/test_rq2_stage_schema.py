import csv
import hashlib
import json
import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
SCHEMA_PATH = ROOT / "tests" / "fixtures" / "rq2_synthetic_pipeline_v2" / "output_schema_v2.json"
sys.path.insert(0, str(SCRIPT_DIR))

import rq2_stage_schema as schema


SHA = "A" * 40


def empty_row(filename):
    row = {field: "" for field in schema.table_contract(filename).fields}
    row["schema_version"] = str(schema.SCHEMA_VERSION)
    return row


class ContractTest(unittest.TestCase):
    def test_code_contract_matches_design_oracle(self):
        design = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        actual = schema.schema_definition()
        self.assertEqual(design["schema_version"], actual["schema_version"])
        self.assertEqual(design["tables"], actual["tables"])
        self.assertEqual(design["enums"], actual["enums"])

    def test_contract_mapping_is_read_only(self):
        with self.assertRaises(TypeError):
            schema.TABLE_CONTRACTS["extra.csv"] = object()

    def test_unknown_contract_is_rejected(self):
        with self.assertRaisesRegex(KeyError, "unknown v2 table"):
            schema.table_contract("unknown.csv")


class StableIdTest(unittest.TestCase):
    def test_commit_id_normalizes_repo_and_sha(self):
        first = schema.make_commit_id("Owner\\Repo/", SHA)
        second = schema.make_commit_id("owner/repo", SHA.lower())
        self.assertEqual(first, second)
        self.assertRegex(first, r"^cmt_[0-9a-f]{32}$")

    def test_commit_id_rejects_invalid_inputs(self):
        with self.assertRaisesRegex(ValueError, "owner/name"):
            schema.make_commit_id("repo-only", SHA)
        with self.assertRaisesRegex(ValueError, "hexadecimal"):
            schema.make_commit_id("owner/repo", "not-a-sha")

    def test_event_id_normalizes_path_but_preserves_event_identity(self):
        commit_id = schema.make_commit_id("owner/repo", SHA)
        first = schema.make_event_id(
            commit_id, "IMPORT", "src\\.\\main.ts", 12, 0, "left-pad"
        )
        second = schema.make_event_id(
            commit_id, "import", "src/main.ts", 12, 0, "left-pad"
        )
        changed = schema.make_event_id(
            commit_id, "import", "src/main.ts", 13, 0, "left-pad"
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        self.assertRegex(first, r"^evt_[0-9a-f]{32}$")

    def test_event_id_rejects_parent_path_and_invalid_ordinal(self):
        commit_id = schema.make_commit_id("owner/repo", SHA)
        with self.assertRaisesRegex(ValueError, "repository-relative"):
            schema.make_event_id(commit_id, "import", "../x.py", 1, 0, "x")
        with self.assertRaisesRegex(ValueError, "at least 0"):
            schema.make_event_id(commit_id, "import", "x.py", 1, -1, "x")

    def test_episode_id_uses_unicode_nfc_without_lowering_identity(self):
        commit_id = schema.make_commit_id("owner/repo", SHA)
        composed = "café"
        decomposed = unicodedata.normalize("NFD", composed)
        first = schema.make_episode_id(
            commit_id, "PyPI", composed, "name_only", ""
        )
        second = schema.make_episode_id(
            commit_id, "pypi", decomposed, "NAME_ONLY", ""
        )
        different_case = schema.make_episode_id(
            commit_id, "pypi", "Café", "name_only", ""
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, different_case)
        self.assertRegex(first, r"^dep_[0-9a-f]{32}$")

    def test_exact_and_range_queries_require_values(self):
        commit_id = schema.make_commit_id("owner/repo", SHA)
        for kind in ("exact", "range"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "requires query_value"):
                    schema.make_episode_id(commit_id, "npm", "left-pad", kind, "")

    def test_evidence_id_normalizes_digest_case(self):
        digest = hashlib.sha256(b"payload").hexdigest()
        first = schema.make_evidence_id("OSV", "npm:left-pad@1.3.0", digest.upper())
        second = schema.make_evidence_id("osv", "npm:left-pad@1.3.0", digest)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^evd_[0-9a-f]{32}$")
        with self.assertRaisesRegex(ValueError, "64 hexadecimal"):
            schema.make_evidence_id("osv", "query", "bad")


class RowValidationTest(unittest.TestCase):
    def test_valid_rows_cover_all_four_tables(self):
        rows = {
            "commits.csv": {"commit_id": "cmt_1"},
            "events.csv": {"event_id": "evt_1", "event_type": "import"},
            "episodes.csv": {
                "episode_id": "dep_1", "query_kind": "name_only",
                "registry_status": "cannot_compare", "advisory_status": "not_queried",
            },
            "episode_event_links.csv": {"episode_id": "dep_1", "event_id": "evt_1"},
        }
        for filename, values in rows.items():
            with self.subTest(table=filename):
                row = empty_row(filename)
                row.update(values)
                schema.validate_row(filename, row)

    def test_missing_extra_and_empty_primary_key_are_rejected(self):
        row = empty_row("commits.csv")
        row["commit_id"] = "cmt_1"
        del row["agent"]
        row["unexpected"] = "x"
        with self.assertRaisesRegex(ValueError, "missing=.*agent.*extra=.*unexpected"):
            schema.validate_row("commits.csv", row)
        row = empty_row("commits.csv")
        with self.assertRaisesRegex(ValueError, "primary key"):
            schema.validate_row("commits.csv", row)

    def test_version_and_enum_are_rejected(self):
        row = empty_row("events.csv")
        row.update({"event_id": "evt_1", "event_type": "unknown"})
        with self.assertRaisesRegex(ValueError, "invalid event_type"):
            schema.validate_row("events.csv", row)
        row["event_type"] = "import"
        row["schema_version"] = "1"
        with self.assertRaisesRegex(ValueError, "schema_version must be 2"):
            schema.validate_row("events.csv", row)


class ManifestTest(unittest.TestCase):
    def test_manifest_has_hash_rows_fields_and_primary_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            for filename, contract in schema.TABLE_CONTRACTS.items():
                self._write_table(output_dir / filename, contract.fields, 2)
            manifest = schema.build_schema_manifest(output_dir)
            self.assertEqual(4, manifest["table_count"])
            for filename, metadata in manifest["tables"].items():
                with self.subTest(table=filename):
                    self.assertEqual(2, metadata["row_count"])
                    self.assertRegex(metadata["sha256"], r"^[0-9a-f]{64}$")
                    self.assertEqual(
                        list(schema.table_contract(filename).fields), metadata["fields"]
                    )

    def test_manifest_rejects_missing_file_and_wrong_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            with self.assertRaisesRegex(FileNotFoundError, "missing v2 table"):
                schema.build_schema_manifest(output_dir, ["commits.csv"])
            self._write_table(output_dir / "commits.csv", ("wrong",), 0)
            with self.assertRaisesRegex(ValueError, "invalid header"):
                schema.build_schema_manifest(output_dir, ["commits.csv"])

    @staticmethod
    def _write_table(path, fields, row_count):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for index in range(row_count):
                writer.writerow({field: f"{field}-{index}" for field in fields})


if __name__ == "__main__":
    unittest.main()

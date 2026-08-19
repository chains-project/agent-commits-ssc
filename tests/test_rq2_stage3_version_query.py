import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_DIR))

import rq2_stage3_version_query as version_query


AUTHOR = "2026-07-01T00:00:00Z"


def episode(kind, value="", ecosystem="npm"):
    return {
        "query_kind": kind, "query_value": value, "ecosystem": ecosystem,
    }


def snapshot(versions=None, status="ok"):
    return {"lookup_status": status, "versions": versions or []}


def release(version, published_at):
    return {"version": version, "published_at": published_at}


class RegistryDecisionTest(unittest.TestCase):
    def test_parse_time_accepts_rfc3339_nanoseconds(self):
        parsed = version_query.parse_time("2026-07-09T16:45:06.281535670Z")
        self.assertEqual(parsed.isoformat(), "2026-07-09T16:45:06.281535+00:00")
        offset = version_query.parse_time(
            "2026-07-09T18:45:06.281535670+02:00"
        )
        self.assertEqual(offset, parsed)

    def test_exact_exists_before_and_after_author_date(self):
        before = snapshot([release("1.3.0", "2020-01-01T00:00:00Z")])
        after = snapshot([release("1.3.0", "2027-01-01T00:00:00Z")])
        self.assertEqual(
            version_query.evaluate_registry(episode("exact", "1.3.0"), AUTHOR, before).status,
            "exists_before_author_date",
        )
        decision = version_query.evaluate_registry(episode("exact", "1.3.0"), AUTHOR, after)
        self.assertEqual(decision.status, "published_after_author_date")
        self.assertIn("history_validation_required", decision.reasons)

    def test_current_absence_remains_weak(self):
        decision = version_query.evaluate_registry(
            episode("exact", "9.9.9"), AUTHOR,
            snapshot([release("1.3.0", "2020-01-01T00:00:00Z")]),
        )
        self.assertEqual(decision.status, "current_absent_unknown")
        self.assertIn("unknown_deleted_or_unpublished_possible", decision.reasons)
        self.assertIn("registry_requested_version_absent", decision.reasons)
        not_found = version_query.evaluate_registry(episode("name_only"), AUTHOR, snapshot(status="not_found"))
        self.assertEqual(not_found.status, "current_absent_unknown")
        self.assertIn("unknown_deleted_or_unpublished_possible", not_found.reasons)
        self.assertIn("registry_package_not_found_at_query", not_found.reasons)

    def test_empty_registry_history_has_specific_weak_reason(self):
        for kind, value in (("name_only", ""), ("exact", "1.0.0"), ("range", "^1.0.0")):
            with self.subTest(kind=kind):
                decision = version_query.evaluate_registry(
                    episode(kind, value), AUTHOR, snapshot([])
                )
                self.assertEqual(decision.status, "current_absent_unknown")
                self.assertIn("unknown_deleted_or_unpublished_possible", decision.reasons)
                self.assertIn("registry_release_history_empty", decision.reasons)

    def test_registry_lookup_failure_stays_cannot_compare(self):
        decision = version_query.evaluate_registry(
            episode("name_only"), AUTHOR, snapshot(status="timeout")
        )
        self.assertEqual(decision.status, "cannot_compare")
        self.assertEqual(decision.reasons, ("registry_timeout",))

    def test_name_only_uses_release_history(self):
        decision = version_query.evaluate_registry(
            episode("name_only"), AUTHOR,
            snapshot([release("0.1.0", "2020-01-01T00:00:00Z")]),
        )
        self.assertEqual(decision.status, "exists_before_author_date")

    def test_npm_range_match_no_match_and_invalid(self):
        versions = snapshot([
            release("1.3.0", "2020-01-01T00:00:00Z"),
            release("2.0.0", "2027-01-01T00:00:00Z"),
        ])
        matched = version_query.evaluate_registry(episode("range", "^1.2.0"), AUTHOR, versions)
        no_match = version_query.evaluate_registry(episode("range", "^9.0.0"), AUTHOR, versions)
        invalid = version_query.evaluate_registry(episode("range", ">=1 <"), AUTHOR, versions)
        self.assertEqual(matched.status, "exists_before_author_date")
        self.assertEqual(no_match.status, "no_matching_version_before_author_date")
        self.assertEqual(invalid.status, "cannot_compare")
        self.assertIn("unsupported_range_syntax", invalid.reasons)

    def test_npm_dist_tag_is_unsupported_not_wildcard(self):
        versions = snapshot([release("6.0.0-dev.20260804", "2026-08-04T00:00:00Z")])
        decision = version_query.evaluate_registry(
            episode("range", "next"), AUTHOR, versions
        )
        self.assertEqual(decision.status, "cannot_compare")
        self.assertIn("unsupported_range_syntax", decision.reasons)
        self.assertIsNone(version_query.build_range_matcher("npm", "next"))
        self.assertIsNone(version_query.build_range_matcher("npm", "next.x"))

    def test_semver_prerelease_requires_explicit_range(self):
        stable_range = version_query.build_range_matcher("npm", "^1.2.0")
        prerelease_range = version_query.build_range_matcher("npm", "^1.2.0-beta.1")
        self.assertFalse(stable_range("1.3.0-beta.1"))
        self.assertTrue(prerelease_range("1.3.0-beta.1"))

    def test_pypi_cargo_and_maven_ranges(self):
        cases = [
            ("PyPI", ">=2,<3", "2.32.3"),
            ("Cargo", "1.0.0", "1.5.0"),
            ("Cargo", "1.*", "1.5.0"),
            ("Maven", "[3.0,4.0)", "3.14.0"),
        ]
        for ecosystem, spec, value in cases:
            with self.subTest(ecosystem=ecosystem):
                matcher = version_query.build_range_matcher(ecosystem, spec)
                self.assertIsNotNone(matcher)
                self.assertTrue(matcher(value))
        maven = version_query.build_range_matcher("Maven", "[1.0,2.0)")
        self.assertIsNone(maven("1.5.0-SNAPSHOT"))
        self.assertIsNone(version_query.build_range_matcher("Maven", "[1.0-alpha,2.0)"))

    def test_empty_range_snapshot_remains_current_absence(self):
        decision = version_query.evaluate_registry(
            episode("range", "^1.0.0"), AUTHOR, snapshot([])
        )
        self.assertEqual(decision.status, "current_absent_unknown")
        self.assertIn("registry_release_history_empty", decision.reasons)

    def test_missing_publish_time_cannot_compare(self):
        decision = version_query.evaluate_registry(
            episode("exact", "1.3.0"), AUTHOR,
            snapshot([release("1.3.0", "")]),
        )
        self.assertEqual(decision.status, "cannot_compare")
        self.assertIn("registry_publish_time_missing", decision.reasons)


if __name__ == "__main__":
    unittest.main()

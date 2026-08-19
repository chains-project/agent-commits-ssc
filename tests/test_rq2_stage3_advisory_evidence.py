import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_DIR))

import rq2_stage3_advisory_evidence as advisory


AUTHOR = "2026-07-01T00:00:00Z"


def record(advisories=None, status="ok", **values):
    return {
        "evidence_type": "advisory", "source": "OSV", "ecosystem": "npm",
        "package_name": "left-pad", "query_kind": "exact", "query_value": "1.3.0",
        "lookup_status": status, "queried_at": "2026-08-03T00:00:00Z",
        "advisories": advisories or [], **values,
    }


def osv(published, withdrawn="", malicious=False, **values):
    return {
        "id": values.get("id", "OSV-1"), "published": published,
        "withdrawn": withdrawn, "is_malicious": malicious,
        "hydration_status": values.get("hydration_status", "ok"),
    }


class EvidenceBundleTest(unittest.TestCase):
    def test_identical_record_is_deduplicated(self):
        evidence = record()
        bundle = advisory.build_evidence_bundle([evidence, dict(evidence)])
        self.assertEqual(len(bundle.items), 1)
        self.assertEqual(len(bundle.payloads), 1)
        item = bundle.items[0]
        self.assertRegex(item.evidence_id, r"^evd_[0-9a-f]{32}$")
        self.assertEqual(tuple(item.metadata), (
            "evidence_id", "evidence_type", "query_key", "source", "lookup_status",
            "queried_at", "payload_sha256", "payload_path", "error", "schema_version",
        ))

    def test_conflicting_same_query_is_rejected(self):
        first = record()
        second = record(error="changed")
        with self.assertRaisesRegex(ValueError, "conflicting evidence"):
            advisory.build_evidence_bundle([first, second])


class AdvisoryDecisionTest(unittest.TestCase):
    def test_active_and_malicious_advisory(self):
        decision = advisory.evaluate_advisory(
            AUTHOR, record([osv("2020-01-01T00:00:00Z", malicious=True)])
        )
        self.assertEqual(decision.status, "active_before_or_at_author_date")
        self.assertIn("malicious_advisory_active", decision.reasons)

    def test_future_and_withdrawn_lifecycle(self):
        future = advisory.evaluate_advisory(
            AUTHOR, record([osv("2027-01-01T00:00:00Z")])
        )
        withdrawn = advisory.evaluate_advisory(
            AUTHOR, record([osv("2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z")])
        )
        self.assertEqual(future.status, "published_after_author_date")
        self.assertEqual(withdrawn.status, "withdrawn_before_author_date")

    def test_none_requires_complete_hydration(self):
        self.assertEqual(advisory.evaluate_advisory(AUTHOR, record()).status, "none_observed")
        incomplete = advisory.evaluate_advisory(AUTHOR, record(status="hydration_incomplete"))
        self.assertEqual(incomplete.status, "hydration_incomplete")
        self.assertIn("osv_hydration_failed", incomplete.reasons)

    def test_confirmed_active_survives_partial_hydration(self):
        evidence = record(
            [osv("2020-01-01T00:00:00Z"), osv("", hydration_status="failed", id="OSV-2")],
            status="hydration_incomplete",
        )
        decision = advisory.evaluate_advisory(AUTHOR, evidence)
        self.assertEqual(decision.status, "active_before_or_at_author_date")
        self.assertIn("advisory_hydration_partial", decision.reasons)

    def test_unsupported_query_remains_not_queried(self):
        decision = advisory.evaluate_advisory(AUTHOR, record(status="unsupported"))
        self.assertEqual(decision.status, "not_queried")
        self.assertIn("advisory_query_unsupported", decision.reasons)


if __name__ == "__main__":
    unittest.main()

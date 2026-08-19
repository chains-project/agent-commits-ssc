import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_DIR))

import rq2_stage2_alignment as alignment


def event(event_type, ecosystem, package, version="", path="package.json", **values):
    row = {
        "event_id": values.get("event_id", "evt_1"), "commit_id": "cmt_1",
        "event_type": event_type, "path": path,
        "actual_language": values.get("actual_language", "TypeScript"),
        "ecosystem": ecosystem, "new_line_number": "1", "event_ordinal": "0",
        "raw_target": values.get("raw_target", package),
        "package_candidate": package, "version_spec": version,
        "event_status": values.get("event_status", "direct_manifest"),
        "reason_codes": values.get("reason_codes", ""), "schema_version": "2",
    }
    return row


class QueryPlanTest(unittest.TestCase):
    def test_npm_alias_separates_declared_and_query_identity(self):
        plan = alignment.plan_manifest(event(
            "manifest_addition", "npm", "legacy-name", "npm:left-pad@^1.3.0"
        ))
        self.assertEqual(plan.declared_identity, "legacy-name")
        self.assertEqual(plan.package_name, "left-pad")
        self.assertEqual((plan.query_kind, plan.query_value), ("range", "^1.3.0"))
        self.assertIn("npm_alias", plan.reasons)

    def test_npm_protocol_version_keeps_declared_identity(self):
        plan = alignment.plan_manifest(event(
            "manifest_addition", "npm", "tiged", "npm:2.12.3"
        ))
        self.assertEqual(plan.declared_identity, "tiged")
        self.assertEqual(plan.package_name, "tiged")
        self.assertEqual((plan.query_kind, plan.query_value), ("exact", "2.12.3"))
        self.assertEqual(plan.resolution_source, "npm_protocol_declared")
        self.assertIn("npm_protocol", plan.reasons)
        self.assertNotIn("npm_alias", plan.reasons)

    def test_scoped_npm_alias_uses_target_package_and_version(self):
        plan = alignment.plan_manifest(event(
            "manifest_addition", "npm", "legacy-name", "npm:@scope/pkg@1.2.3"
        ))
        self.assertEqual(plan.package_name, "@scope/pkg")
        self.assertEqual((plan.query_kind, plan.query_value), ("exact", "1.2.3"))
        self.assertEqual(plan.resolution_source, "npm_alias_target")
        self.assertIn("npm_alias", plan.reasons)

    def test_ecosystem_version_semantics(self):
        cases = [
            ("npm", "1.3.0", "exact", "1.3.0"),
            ("PyPI", "==2.32.3", "exact", "2.32.3"),
            ("Cargo", "1.0.203", "range", "1.0.203"),
            ("Cargo", "=1.0.203", "exact", "1.0.203"),
            ("Go", "v1.9.0", "exact", "v1.9.0"),
            ("Maven", "${revision}", "name_only", ""),
        ]
        for ecosystem, spec, kind, value in cases:
            with self.subTest(ecosystem=ecosystem, spec=spec):
                actual = alignment.classify_version(ecosystem, spec)
                self.assertEqual(actual[:2], (kind, value))

    def test_private_local_spec_is_excluded(self):
        plan = alignment.plan_manifest(event(
            "manifest_addition", "npm", "local-package", "file:../local"
        ))
        self.assertEqual(plan.query_kind, "excluded")
        self.assertIn("private_or_local", plan.reasons)

    def test_cargo_workspace_inheritance_is_excluded_from_public_query(self):
        plan = alignment.plan_manifest(event(
            "manifest_addition", "Cargo", "tokio", "workspace:",
            reason_codes="workspace_inherited",
        ))
        self.assertEqual(plan.package_name, "tokio")
        self.assertEqual((plan.query_kind, plan.query_value), ("excluded", ""))
        self.assertIn("workspace_inherited", plan.reasons)
        self.assertIn("private_or_local", plan.reasons)


class ManifestSelectionTest(unittest.TestCase):
    def test_go_uses_module_prefix(self):
        imported = event(
            "import", "Go", "github.com/acme/tool/sub", path="cmd/main.go"
        )
        manifest = event(
            "manifest_addition", "Go", "github.com/acme/tool", "v1.2.3",
            path="go.mod",
        )
        selection = alignment.select_manifests(imported, [manifest])
        self.assertEqual(selection.events, (manifest,))

    def test_nearest_module_wins(self):
        imported = event("import", "npm", "left-pad", path="packages/a/src/a.ts")
        root = event("manifest_addition", "npm", "left-pad", "1.0.0", path="package.json")
        local = event("manifest_addition", "npm", "left-pad", "2.0.0", path="packages/a/package.json")
        selection = alignment.select_manifests(imported, [root, local])
        self.assertEqual(selection.events, (local,))

    def test_alias_and_normalized_alignment_statuses(self):
        npm_import = event("import", "npm", "legacy-name", event_status="direct_mapping")
        npm_alias = event("manifest_addition", "npm", "legacy-name", "npm:left-pad@1.3.0")
        python_alias = event(
            "import", "PyPI", "opencv-python", path="src/app.py",
            raw_target="cv2", event_status="alias_mapping",
        )
        python_manifest = event(
            "manifest_addition", "PyPI", "opencv-python", "==4.10.0.84",
            path="requirements.txt",
        )
        normalized = event(
            "import", "PyPI", "name-with-dash", path="src/app.py",
            raw_target="name_with_dash", event_status="direct_or_normalized_mapping",
        )
        normalized_manifest = event(
            "manifest_addition", "PyPI", "name-with-dash", "==1.0.0",
            path="requirements.txt",
        )
        self.assertEqual(alignment.select_manifests(npm_import, [npm_alias]).alignment_status, "matched_manifest_alias")
        self.assertEqual(alignment.select_manifests(python_alias, [python_manifest]).alignment_status, "matched_manifest_alias")
        self.assertEqual(alignment.select_manifests(normalized, [normalized_manifest]).alignment_status, "matched_manifest_normalized")

    def test_java_requires_unique_nearest_coordinate(self):
        imported = event(
            "import", "Maven", "", path="module/src/main/java/App.java",
            raw_target="org.example.Widget", actual_language="Java",
            event_status="mapping_uncertain", reason_codes="mapping_uncertain",
        )
        one = event("manifest_addition", "Maven", "org.example:core", "1.0.0", path="module/pom.xml")
        two = event("manifest_addition", "Maven", "org.example:api", "1.0.0", path="module/pom.xml")
        unique = alignment.select_manifests(imported, [one])
        ambiguous = alignment.select_manifests(imported, [one, two])
        self.assertEqual(unique.alignment_status, "matched_manifest_unique_coordinate")
        self.assertEqual(ambiguous.events, ())
        self.assertIn("multiple_package_candidates", ambiguous.reasons)


if __name__ == "__main__":
    unittest.main()

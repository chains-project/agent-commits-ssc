import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_DIR))

import rq2_stage1_manifest_diff as stage1


def patch(path, lines):
    body = [
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    body.extend(f"+{line}" for line in lines)
    return body


class UnifiedDiffTest(unittest.TestCase):
    def test_added_line_numbers_and_multiple_files(self):
        lines = patch("src/a.ts", ["import x from 'left-pad';"])
        lines += patch("tools/check.py", ["import requests"])
        files = stage1.parse_unified_diff(lines)
        self.assertEqual([item.path for item in files], ["src/a.ts", "tools/check.py"])
        self.assertEqual(files[0].added_lines[0].new_line_number, 1)
        self.assertTrue(files[0].added_lines[0].is_added)

    def test_source_language_uses_path_not_repo_metadata(self):
        self.assertEqual(stage1.detect_source_language("tools/check.py"), "Python")
        self.assertEqual(stage1.detect_source_language("src/index.tsx"), "TypeScript")
        self.assertEqual(stage1.detect_source_language("src/index.js"), "JavaScript")
        self.assertEqual(stage1.detect_source_language("src/lib.rs"), "Rust")
        self.assertEqual(stage1.detect_source_language("cmd/main.go"), "Go")
        self.assertEqual(stage1.detect_source_language("src/App.java"), "Java")

    def test_non_research_paths_do_not_count_as_changed_languages(self):
        lines = patch("node_modules/pkg/index.ts", ["import value from 'left-pad';"])
        lines += patch("src/index.ts", ["import value from 'left-pad';"])
        files = stage1.parse_unified_diff(lines)
        self.assertEqual(stage1.actual_changed_languages(files), ("TypeScript",))
        self.assertFalse(stage1.is_research_file_path("node_modules/pkg/index.ts"))
        self.assertTrue(stage1.is_research_file_path("src/index.ts"))


class SourceImportTest(unittest.TestCase):
    def observations(self, path, lines):
        return stage1.extract_stage1_observations(patch(path, lines))

    def test_javascript_multiple_imports_on_one_line(self):
        events = self.observations(
            "src/index.js",
            ["const a=require('left-pad'), b=require('axios');"],
        )
        self.assertEqual([event.package_candidate for event in events], ["left-pad", "axios"])
        self.assertEqual([event.event_ordinal for event in events], [0, 1])

    def test_builtin_and_relative_imports_are_controls(self):
        events = self.observations(
            "src/index.ts",
            ["import fs from 'fs';", "import local from './local';"],
        )
        self.assertEqual(events, [])

    def test_node_builtin_roots_protocol_and_subpaths_are_controls(self):
        events = self.observations(
            "src/index.ts",
            [
                "import workers from 'worker_threads';",
                "import test from 'node:test';",
                "import sqlite from 'node:sqlite';",
                "import promises from 'fs/promises';",
            ],
        )
        self.assertEqual(events, [])

    def test_virtual_and_url_protocol_imports_are_controls(self):
        events = self.observations(
            "src/index.ts",
            [
                "import test from 'bun:test';",
                "import content from 'astro:content';",
                "import value from 'virtual:generated-module';",
                "import remote from 'https://example.com/mod.js';",
                "import jsr from 'jsr:@scope/pkg';",
            ],
        )
        self.assertEqual(events, [])

    def test_workspace_aliases_are_retained_as_unresolved_evidence(self):
        events = self.observations(
            "src/index.ts",
            [
                "import local from '$lib/server';",
                "import db from '@workspace/db';",
                "import core from '@repo/core';",
                "import shared from '@packages/shared';",
            ],
        )
        self.assertEqual(len(events), 4)
        self.assertEqual({event.package_candidate for event in events}, {""})
        self.assertEqual({event.event_status for event in events}, {"mapping_uncertain"})
        self.assertEqual(
            {event.reason_codes for event in events},
            {("possible_workspace_alias",)},
        )

    def test_real_scoped_package_remains_direct_mapping(self):
        events = self.observations(
            "src/index.ts",
            ["import common from '@nestjs/common';"],
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].package_candidate, "@nestjs/common")
        self.assertEqual(events[0].event_status, "direct_mapping")

    def test_invalid_npm_package_candidates_remain_unresolved(self):
        events = self.observations(
            "src/index.js",
            ["const a = require(',');", "const b = require('+u+');"],
        )
        self.assertEqual(len(events), 2)
        self.assertEqual({event.package_candidate for event in events}, {""})
        self.assertEqual({event.event_status for event in events}, {"mapping_uncertain"})
        self.assertEqual(
            {event.reason_codes for event in events},
            {("invalid_npm_package_candidate",)},
        )

    def test_valid_npm_name_forms_remain_direct(self):
        events = self.observations(
            "src/index.js",
            [
                "const a = require('lodash.get');",
                "const b = require('@scope/pkg/subpath');",
                "const c = require('package-name/subpath');",
            ],
        )
        self.assertEqual(
            [event.package_candidate for event in events],
            ["lodash.get", "@scope/pkg", "package-name"],
        )

    def test_hyphenated_shell_argument_is_not_a_from_import(self):
        events = self.observations(
            "packages/react-doctor/tests/github-action.test.ts",
            ['--changed-files-from" "$CHANGED_FILES_FROM"'],
        )
        self.assertEqual(events, [])

    def test_multiline_from_clause_remains_supported(self):
        events = self.observations("src/index.ts", ['} from "left-pad";'])
        self.assertEqual([event.package_candidate for event in events], ["left-pad"])

    def test_python_alias_and_multilanguage_detection(self):
        lines = patch("src/index.ts", ["import x from 'left-pad';"])
        lines += patch("tools/check.py", ["import cv2"])
        files = stage1.parse_unified_diff(lines)
        events = stage1.extract_stage1_observations(lines)
        self.assertEqual(stage1.actual_changed_languages(files), ("TypeScript", "Python"))
        self.assertEqual({event.package_candidate for event in events}, {"left-pad", "opencv-python"})

    def test_python_stdlib_is_excluded_without_runtime_module_catalog(self):
        events = self.observations(
            "src/control.py",
            ["import sys", "import pathlib", "import json"],
        )
        self.assertEqual(events, [])

    def test_java_import_is_retained_as_mapping_uncertain(self):
        events = self.observations(
            "src/App.java",
            ["import org.apache.commons.lang3.StringUtils;"],
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].package_candidate, "")
        self.assertEqual(events[0].reason_codes, ("mapping_uncertain",))


class ManifestAdditionTest(unittest.TestCase):
    def observations(self, path, lines):
        return stage1.extract_stage1_observations(patch(path, lines))

    def test_npm_manifest_only(self):
        events = self.observations(
            "package.json",
            ['"dependencies": {', '  "left-pad": "1.3.0"', '}'],
        )
        self.assertEqual([(event.package_candidate, event.version_spec) for event in events], [("left-pad", "1.3.0")])

    def test_python_requirements_manifest_only(self):
        events = self.observations("requirements.txt", ["requests==2.32.3"])
        self.assertEqual(events[0].package_candidate, "requests")
        self.assertEqual(events[0].version_spec, "==2.32.3")

    def test_requirements_inline_comment_is_not_a_version(self):
        events = self.observations(
            "requirements.txt",
            ["tempfile  # Built-in", "requests>=2.32.3  # HTTP client"],
        )
        self.assertEqual(
            [(event.package_candidate, event.version_spec) for event in events],
            [("tempfile", ""), ("requests", ">=2.32.3")],
        )
    def test_requirements_prose_is_not_a_dependency(self):
        events = self.observations(
            "requirements.txt", ["Buy Now CTA: #f57224 (Daraz orange)"]
        )
        self.assertEqual(events, [])
    def test_pyproject_commented_array_dependency_is_ignored(self):
        events = self.observations(
            "pyproject.toml",
            [
                "[project]",
                "dependencies = [",
                '  # "conda >=25.9.0",',
                '  "requests >=2.32.3",',
                "]",
            ],
        )
        self.assertEqual(
            [(event.package_candidate, event.version_spec) for event in events],
            [("requests", ">=2.32.3")],
        )

    def test_setup_cfg_install_requires_stops_at_next_option(self):
        events = self.observations(
            "setup.cfg",
            [
                "[options]", "install_requires =", "    requests==2.32.3",
                "python_requires = >=3.9", "    numpy==2.0.0",
            ],
        )
        self.assertEqual(
            [(event.package_candidate, event.version_spec) for event in events],
            [("requests", "==2.32.3")],
        )

    def test_setup_cfg_inline_install_requires_is_parsed(self):
        events = self.observations(
            "setup.cfg", ["[options]", "install_requires = requests==2.32.3"],
        )
        self.assertEqual(
            [(event.package_candidate, event.version_spec) for event in events],
            [("requests", "==2.32.3")],
        )

    def test_rust_manifest_only(self):
        events = self.observations("Cargo.toml", ["[dependencies]", 'serde = "1.0.203"'])
        self.assertEqual((events[0].package_candidate, events[0].version_spec), ("serde", "1.0.203"))

    def test_cargo_dotted_workspace_keeps_identity_without_true_version(self):
        events = self.observations(
            "crates/server/Cargo.toml",
            ["[dependencies]", "anyhow.workspace = true"],
        )
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].package_candidate, events[0].version_spec), ("anyhow", "workspace:"))
        self.assertEqual(events[0].reason_codes, ("workspace_inherited",))

    def test_cargo_inline_workspace_is_not_a_public_version_range(self):
        events = self.observations(
            "crates/server/Cargo.toml",
            ["[dependencies]", 'tokio = { workspace = true, features = ["test-util"] }'],
        )
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].package_candidate, events[0].version_spec), ("tokio", "workspace:"))
        self.assertEqual(events[0].reason_codes, ("workspace_inherited",))

    def test_cargo_dotted_feature_only_does_not_add_dependency(self):
        events = self.observations(
            "Cargo.toml",
            ["[dependencies]", 'tokio.features = ["rt-multi-thread"]'],
        )
        self.assertEqual(events, [])

    def test_cargo_inline_version_remains_queryable(self):
        events = self.observations(
            "Cargo.toml",
            ["[dependencies]", 'serde = { version = "1.0.203", features = ["derive"] }'],
        )
        self.assertEqual((events[0].package_candidate, events[0].version_spec), ("serde", "1.0.203"))

    def test_go_manifest_only(self):
        events = self.observations(
            "go.mod",
            ["require (", "  github.com/stretchr/testify v1.9.0", ")"],
        )
        self.assertEqual(
            (events[0].package_candidate, events[0].version_spec),
            ("github.com/stretchr/testify", "v1.9.0"),
        )

    def test_maven_manifest_only(self):
        events = self.observations(
            "pom.xml",
            [
                "<dependencies>", "<dependency>",
                "<groupId>org.apache.commons</groupId>",
                "<artifactId>commons-lang3</artifactId>",
                "<version>3.14.0</version>", "</dependency>", "</dependencies>",
            ],
        )
        self.assertEqual(
            (events[0].package_candidate, events[0].version_spec),
            ("org.apache.commons:commons-lang3", "3.14.0"),
        )

    def test_gradle_coordinate_and_catalog(self):
        gradle = self.observations(
            "build.gradle", ["dependencies {", "implementation 'org.apache.commons:commons-lang3:3.14.0'", "}"],
        )
        catalog = self.observations(
            "gradle/libs.versions.toml",
            ["[libraries]", 'commons = { module = "org.apache.commons:commons-lang3", version = "3.14.0" }'],
        )
        self.assertEqual(gradle[0].package_candidate, "org.apache.commons:commons-lang3")
        self.assertEqual(catalog[0].package_candidate, "org.apache.commons:commons-lang3")

    def test_gradle_local_project_dependency_is_not_external_maven(self):
        cases = (
            ("build.gradle", "implementation project(':client:feature:timeline')"),
            ("build.gradle.kts", 'implementation(project(":client:feature:timeline"))'),
            ("build.gradle.kts", 'api(project(path = ":shared:util:core"))'),
        )
        for path, line in cases:
            with self.subTest(path=path, line=line):
                self.assertEqual(self.observations(path, [line]), [])

    def test_gradle_non_dependency_strings_do_not_trigger_configuration(self):
        lines = (
            'throw GradleException("Run :bot-api:dotnet:build first")',
            'dependsOn(":bot-api:dotnet:build")',
            'logger.warn("implementation org.example:fake:1.0")',
        )
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(self.observations("build.gradle.kts", [line]), [])

    def test_gradle_real_dependency_dsl_forms_remain_external_maven(self):
        cases = (
            ('modImplementation "net.fabricmc.fabric-api:fabric-api:0.92.2"',
             "net.fabricmc.fabric-api:fabric-api"),
            ('compile "org.slf4j:slf4j-api:2.0.17"', "org.slf4j:slf4j-api"),
            ("testFixturesImplementation 'org.junit.jupiter:junit-jupiter-api:6.0.1'",
             "org.junit.jupiter:junit-jupiter-api"),
            ('include implementation("com.zaxxer:HikariCP:7.0.2")',
             "com.zaxxer:HikariCP"),
            ("jarJar(implementation('com.mysql:mysql-connector-j:8.0.33'))",
             "com.mysql:mysql-connector-j"),
            ('thirdParty("com.google.api-client:google-api-client:2.6.0")',
             "com.google.api-client:google-api-client"),
            ('"implementation"(platform("io.netty:netty-bom:4.1.132.Final"))',
             "io.netty:netty-bom"),
        )
        for line, package in cases:
            with self.subTest(line=line):
                events = self.observations("build.gradle.kts", [line])
                self.assertEqual([event.package_candidate for event in events], [package])

    def test_gradle_configuration_expression_wrappers_remain_dependencies(self):
        cases = (
            ('implementation fg.deobf("net.createmod.ponder:Ponder-Forge-1.20.1:1.0.80")',
             "net.createmod.ponder:Ponder-Forge-1.20.1"),
            ('compileOnly fg.deobf("dev.emi:emi-forge:1.1.22+1.20.1:api")',
             "dev.emi:emi-forge"),
            ('runtimeOnly fg.deobf("maven.modrinth:create:1.20.1-6.0.6")',
             "maven.modrinth:create"),
            ('implementation include("com.squareup.okio:okio-jvm:3.6.0")',
             "com.squareup.okio:okio-jvm"),
        )
        for line, package in cases:
            with self.subTest(line=line):
                events = self.observations("build.gradle", [line])
                self.assertEqual([event.package_candidate for event in events], [package])

    def test_gradle_nested_and_derived_configuration_aliases_remain(self):
        cases = (
            ("commonTest.dependencies { implementation(libs.kotlin.test) }",
             "libs.kotlin.test"),
            ("screenshotTestImplementation(libs.screenshot.validation.api)",
             "libs.screenshot.validation.api"),
            ("integrationTestImplementation(libs.junit.jupiter.api)",
             "libs.junit.jupiter.api"),
        )
        for line, package in cases:
            with self.subTest(line=line):
                events = self.observations("build.gradle.kts", [line])
                self.assertEqual([event.package_candidate for event in events], [package])

    def test_gradle_unrelated_helper_strings_do_not_become_dependencies(self):
        lines = (
            '// implementation("org.example:commented:1.0")',
            'buildConfigField("String", "API_BASE_URL", "http://10.0.2.2:8000/api")',
            'val conn = URI.create("http://localhost:$port/api/action/RestartIde")',
            'relocate("com.google.api", "com.mcgpt.libs.api")',
            'apiDocsUrl.set("http://localhost:8080/v3/api-docs")',
        )
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(self.observations("build.gradle.kts", [line]), [])

    def test_lockfile_is_not_manifest_addition(self):
        self.assertEqual(self.observations("package-lock.json", ['"left-pad": "1.3.0"']), [])

    def test_dependency_installation_vendored_and_generated_paths_are_excluded(self):
        excluded = (
            "node_modules/pkg/package.json",
            "vendor/pkg/package.json",
            "vendored/pkg/package.json",
            "generated/client/package.json",
        )
        lines = ['"dependencies": {', '"left-pad": "1.3.0"', '}']
        for path in excluded:
            with self.subTest(path=path):
                self.assertEqual(self.observations(path, lines), [])

    def test_test_and_fixture_paths_remain_in_scope(self):
        lines = ['"dependencies": {', '"left-pad": "1.3.0"', '}']
        for path in (
            "tests/package.json",
            "tests/fixtures/parser/package.json",
            "e2e/fixture/package.json",
        ):
            with self.subTest(path=path):
                events = self.observations(path, lines)
                self.assertEqual([event.package_candidate for event in events], ["left-pad"])

    def test_quadrants_cover_entry_cases(self):
        import_event = self.observations("src/index.ts", ["import x from 'left-pad';"])
        manifest_event = self.observations(
            "package.json", ['"dependencies": {', '"left-pad": "1.3.0"', '}'],
        )
        self.assertEqual(stage1.dependency_quadrant([]), "I-M-")
        self.assertEqual(stage1.dependency_quadrant(import_event), "I+M-")
        self.assertEqual(stage1.dependency_quadrant(manifest_event), "I-M+")
        self.assertEqual(stage1.dependency_quadrant(import_event + manifest_event), "I+M+")


if __name__ == "__main__":
    unittest.main()

"""Materialize deterministic offline inputs for RQ2 Stage 4 validation.

[IN]: One v2 synthetic scenario with abstract source and manifest events.
[OUT]: Local unified diff/index/population files and fixed Stage 3 evidence.
[POS]: Test-fixture adapter only; it never accesses a network or writes thesis data.
[SYNC]: Keep run_rq2_stage4_synthetic_chain.py and the v2 scenario oracle aligned.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Sequence


QUERY_TIME = "2026-08-03T00:00:00Z"
RELEASE_TIME = "2018-01-01T00:00:00Z"
RELEASES = {
    ("npm", "left-pad"): ("1.3.0",),
    ("npm", "axios"): ("1.6.0",),
    ("npm", "minimist"): ("0.0.8",),
    ("PyPI", "opencv-python"): ("4.10.0.84",),
    ("PyPI", "requests"): ("2.32.3",),
    ("PyPI", "numpy"): ("2.0.0",),
    ("Cargo", "serde"): ("1.0.203",),
    ("Cargo", "regex"): ("1.10.5",),
    ("Cargo", "anyhow"): ("1.0.86",),
    ("Go", "github.com/stretchr/testify"): ("v1.9.0",),
    ("Go", "golang.org/x/text"): ("v0.16.0",),
    ("Go", "github.com/google/uuid"): ("v1.6.0",),
    ("Maven", "org.apache.commons:commons-lang3"): ("3.14.0",),
}
SOURCE_NAMES = {
    "TypeScript": "src/control.ts", "JavaScript": "src/control.js",
    "Python": "src/control.py", "Rust": "src/lib.rs",
    "Go": "control.go", "Java": "src/main/java/Control.java",
}
REGISTRY_SOURCES = {
    "npm": "npm", "PyPI": "PyPI", "Cargo": "crates.io",
    "Go": "Go proxy", "Maven": "Maven Central",
}


def scenario_repo(scenario: Mapping[str, object]) -> str:
    return f"synthetic/{scenario['id']}"


def scenario_sha(scenario: Mapping[str, object]) -> str:
    return hashlib.sha1(str(scenario["id"]).encode("utf-8")).hexdigest()


def materialize_stage1_inputs(scenario: Mapping[str, object], root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    patch = root / "commit.diff"
    patch.write_text(render_diff(scenario), encoding="utf-8")
    index = root / "commit_index.csv"
    population = root / "population.csv"
    _write_csv(index, _index_rows(scenario, patch))
    _write_csv(population, _population_rows(scenario))
    return index, population


def render_diff(scenario: Mapping[str, object]) -> str:
    files = _scenario_files(scenario)
    chunks = [_added_file_diff(path, lines) for path, lines in sorted(files.items())]
    return "\n".join(line for chunk in chunks for line in (*chunk, ""))


def _scenario_files(scenario: Mapping[str, object]) -> dict[str, list[str]]:
    inputs = scenario["input"]
    files: dict[str, list[str]] = defaultdict(list)
    for event in inputs.get("source_events", []):
        files[str(event["path"])].append(_source_line(event))
    manifests = _group_by_path(inputs.get("manifest_events", []))
    for path, events in manifests.items():
        files[path].extend(_manifest_lines(path, events))
    for item in inputs.get("tree_dependencies", []):
        if item.get("changed") is True:
            files[str(item["path"])].extend(_tree_dependency_lines(item))
    if not files:
        files[SOURCE_NAMES[str(scenario["language"])]] = [_control_line(str(scenario["language"]))]
    return dict(files)


def _group_by_path(events: Sequence[Mapping[str, object]]) -> dict[str, list[Mapping[str, object]]]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for event in events:
        grouped[str(event["path"])].append(event)
    return dict(grouped)


def _source_line(event: Mapping[str, object]) -> str:
    if event.get("added_line"):
        return str(event["added_line"])
    target = str(event["target"])
    language = str(event["language"])
    templates = {
        "TypeScript": "import value from '{target}';",
        "JavaScript": "const value = require('{target}');",
        "Python": "import {target}", "Rust": "use {target}::value;",
        "Go": 'import "{target}"', "Java": "import {target};",
    }
    return templates[language].format(target=target)


def _control_line(language: str) -> str:
    return {
        "TypeScript": "const value: number = 1;", "JavaScript": "const value = 1;",
        "Python": "value = 1", "Rust": "const VALUE: i32 = 1;",
        "Go": "package control", "Java": "class Control {}",
    }[language]


def _manifest_lines(path: str, events: Sequence[Mapping[str, object]]) -> list[str]:
    name = Path(path).name.lower()
    if name == "package.json":
        return _npm_lines(events)
    if name == "requirements.txt":
        return [f"{row['package']}{row.get('version_spec', '')}" for row in events]
    if name == "cargo.toml":
        return ["[dependencies]", *[f'{row["package"]} = "{row.get("version_spec", "")}"' for row in events]]
    if name == "go.mod":
        return ["module synthetic.example/test", *[f"require {row['package']} {row.get('version_spec', '')}" for row in events]]
    if name == "pom.xml":
        return _maven_lines(events)
    if name.startswith("build.gradle"):
        return _gradle_lines(events)
    raise ValueError(f"unsupported synthetic manifest path: {path}")


def _npm_lines(events: Sequence[Mapping[str, object]]) -> list[str]:
    lines = ['"dependencies": {']
    for row in events:
        spec = str(row.get("version_spec", ""))
        if row.get("target_package"):
            spec = f"npm:{row['target_package']}@{spec}"
        lines.append(f'  "{row["package"]}": "{spec}",')
    return [*lines, "}"]


def _maven_lines(events: Sequence[Mapping[str, object]]) -> list[str]:
    lines = ["<project>", "<dependencies>"]
    for row in events:
        group, artifact = str(row["package"]).split(":", 1)
        lines.extend(["<dependency>", f"<groupId>{group}</groupId>", f"<artifactId>{artifact}</artifactId>"])
        if row.get("version_spec"):
            lines.append(f"<version>{row['version_spec']}</version>")
        lines.append("</dependency>")
    return [*lines, "</dependencies>", "</project>"]


def _gradle_lines(events: Sequence[Mapping[str, object]]) -> list[str]:
    lines = ["dependencies {"]
    for row in events:
        if row.get("added_line"):
            lines.append(str(row["added_line"]))
            continue
        spec = str(row.get("version_spec", ""))
        if spec.startswith("libs."):
            lines.append(f"implementation({spec})")
        else:
            lines.append(f'implementation "{row["package"]}:{spec}"')
    return [*lines, "}"]


def _tree_dependency_lines(item: Mapping[str, object]) -> list[str]:
    return [json.dumps({"packages": {str(item.get("package", "")): {"version": item.get("version_spec", "")}}})]


def _added_file_diff(path: str, lines: Sequence[str]) -> list[str]:
    return [
        f"diff --git a/{path} b/{path}", "new file mode 100644",
        "index 0000000..1111111", "--- /dev/null", f"+++ b/{path}",
        f"@@ -0,0 +1,{len(lines)} @@", *[f"+{line}" for line in lines],
    ]


def _index_rows(scenario: Mapping[str, object], patch: Path) -> list[dict[str, str]]:
    inputs = scenario["input"]
    language_hint = str(inputs.get("commit_languages_hint", scenario["language"]))
    return [{
        "status": "ok", "repo": scenario_repo(scenario), "sha": scenario_sha(scenario),
        "repo_language": str(scenario["repo_primary_language"]),
        "commit_primary_language": str(scenario["language"]),
        "commit_languages": language_hint,
        "patch_path": str(patch.resolve()),
    }]


def _population_rows(scenario: Mapping[str, object]) -> list[dict[str, str]]:
    repo, sha = scenario_repo(scenario), scenario_sha(scenario)
    return [{
        "repo_sha": f"{repo}|{sha}", "repo": repo, "sha": sha,
        "agent": "synthetic_stage4", "author_date": str(scenario["author_date"]),
    }]


def _write_csv(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def materialize_stage2_context(
    scenario: Mapping[str, object],
) -> list[dict[str, str]]:
    inputs = scenario["input"]
    rows = []
    for source in inputs.get("source_events", []):
        rows.extend(_source_context_rows(scenario, source))
    for manifest in inputs.get("manifest_events", []):
        if manifest.get("resolved_version"):
            rows.append(_manifest_context_row(scenario, manifest))
    return rows


def _manifest_context_row(scenario, manifest) -> dict[str, str]:
    source = str(manifest.get("resolution_source", "fixture_context"))
    reason = {
        "local_maven_property": "resolved_local_maven_property",
        "local_gradle_version_catalog": "resolved_local_gradle_catalog",
    }.get(source, "")
    return {
        "repo": scenario_repo(scenario), "sha": scenario_sha(scenario),
        "source_path": str(manifest["path"]), "raw_target": str(manifest["package"]),
        "import_root": str(manifest["package"]),
        "ecosystem": str(manifest["ecosystem"]),
        "package_name": str(manifest["package"]),
        "version_spec": str(manifest.get("version_spec", "")),
        "resolved_version": str(manifest["resolved_version"]),
        "resolution_source": source, "dependency_group": "fixture",
        "dependency_path": str(manifest["path"]),
        "alignment_status": "matched_manifest", "reason_codes": reason,
    }

def _source_context_rows(scenario, source) -> list[dict[str, str]]:
    inputs = scenario["input"]
    tree = list(inputs.get("tree_dependencies", []))
    manifests = list(inputs.get("manifest_events", []))
    candidates = tree or [row for row in manifests if row.get("resolved_version")]
    if not candidates and scenario["id"] == "python_alias_mapping_failed":
        candidates = [{"candidate_packages": []}]
    return [_context_row(scenario, source, item) for item in candidates]


def _context_row(scenario, source, item) -> dict[str, str]:
    packages = list(item.get("candidate_packages", []))
    package = str(item.get("package") or item.get("package_identity") or "")
    status, reasons = _context_status(item, packages)
    if packages:
        package = ""
    resolution = str(item.get("resolution_source") or item.get("source_type") or "fixture_context")
    return {
        "repo": scenario_repo(scenario), "sha": scenario_sha(scenario),
        "source_path": str(source["path"]), "raw_target": str(source["target"]),
        "import_root": str(source["target"]).split("/")[0],
        "ecosystem": _scenario_ecosystem(str(scenario["language"])),
        "package_name": package, "version_spec": str(item.get("version_spec", "")),
        "resolved_version": str(item.get("resolved_version", "")),
        "resolution_source": resolution, "dependency_group": "fixture",
        "dependency_path": str(item.get("path", "")),
        "alignment_status": status, "reason_codes": "|".join(reasons),
    }


def _context_status(item, packages) -> tuple[str, list[str]]:
    if item.get("package_identity"):
        return "first_party_excluded", ["first_party"]
    if packages:
        return "mapping_uncertain", ["mapping_uncertain", "multiple_package_candidates"]
    if item.get("source_type") == "lockfile_only":
        return "lockfile_only_observed", ["possible_transitive_dependency"]
    if item.get("resolved_version"):
        return "matched_manifest_and_lock", []
    if item.get("changed") is False:
        return "matched_existing_manifest", []
    return "mapping_uncertain", ["mapping_uncertain", "python_alias_not_found"]


def _scenario_ecosystem(language: str) -> str:
    return {
        "TypeScript": "npm", "JavaScript": "npm", "Python": "PyPI",
        "Rust": "Cargo", "Go": "Go", "Java": "Maven",
    }[language]

def write_stage3_evidence(
    scenario: Mapping[str, object], episodes: Sequence[Mapping[str, str]], path: Path
) -> None:
    records = []
    registry_keys = set()
    for episode in episodes:
        if episode["query_kind"] not in {"exact", "name_only", "range"}:
            continue
        key = (episode["ecosystem"], episode["package_name"])
        if key not in registry_keys:
            records.append(_registry_record(*key))
            registry_keys.add(key)
        records.append(_advisory_record(scenario, episode))
    text = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in records)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def _registry_record(ecosystem: str, package: str) -> dict[str, object]:
    versions = RELEASES.get((ecosystem, package))
    return {
        "evidence_type": "registry", "source": REGISTRY_SOURCES[ecosystem],
        "ecosystem": ecosystem, "package_name": package,
        "lookup_status": "ok" if versions else "not_found", "queried_at": QUERY_TIME,
        "versions": [{"version": value, "published_at": RELEASE_TIME} for value in versions or ()],
    }


def _advisory_record(
    scenario: Mapping[str, object], episode: Mapping[str, str]
) -> dict[str, object]:
    status, advisories = _advisory_fixture(scenario)
    return {
        "evidence_type": "advisory", "source": "OSV",
        "ecosystem": episode["ecosystem"], "package_name": episode["package_name"],
        "query_kind": episode["query_kind"], "query_value": episode["query_value"],
        "lookup_status": status, "queried_at": QUERY_TIME, "advisories": advisories,
    }


def _advisory_fixture(scenario: Mapping[str, object]) -> tuple[str, list[dict[str, object]]]:
    scenario_id = str(scenario["id"])
    if scenario_id == "known_public_advisory":
        return "ok", [_advisory("SYNTHETIC-PUBLIC")]
    if scenario_id == "known_malicious_advisory":
        return "ok", [_advisory("SYNTHETIC-MALICIOUS", malicious=True)]
    if scenario_id == "osv_withdrawn_before_commit":
        fixture = scenario["input"]["evidence_fixture"]
        return "ok", [_advisory("SYNTHETIC-WITHDRAWN", fixture["published"], fixture["withdrawn"])]
    if scenario_id == "osv_hydration_failed":
        return "hydration_incomplete", [{"id": "SYNTHETIC-HYDRATION-FAILURE", "hydration_status": "failed"}]
    return "ok", []


def _advisory(
    advisory_id: str, published: str = "2020-01-01T00:00:00Z",
    withdrawn: str = "", *, malicious: bool = False,
) -> dict[str, object]:
    return {
        "id": advisory_id, "published": published, "withdrawn": withdrawn,
        "hydration_status": "ok", "is_malicious": malicious,
    }

"""Build final conservative RQ2 hallucination adjudication.

[IN]: Candidate, context, registry, GitHub, temporal, and corrective CSV evidence.
[OUT]: One atomic adjudication CSV with label, confidence, basis, and evidence flags.
[POS]: Versionless public RQ2 final-adjudication module; canonical inputs are read-only.
[SYNC]: Keep tests/test_rq2_adjudication.py and scripts/CLAUDE.md aligned.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import uuid
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Mapping


csv.field_size_limit(1_000_000_000)
OUTPUT_FIELDS = (
    "case_id", "episode_id", "languages_json", "repo", "sha", "agent",
    "ecosystem", "package_name", "query_kind", "query_value",
    "original_registry_status", "final_label", "confidence",
    "adjudication_basis", "evidence_flags_json", "patch_path",
)
INPUT_NAMES = (
    "candidate_csv", "evidence_csv", "context_csv", "python_mapping",
    "python_alternate", "temporal_queue", "temporal_requery",
    "temporal_npm_semver", "current_requery", "current_npm_semver",
    "current_temporal", "maven_requery", "maven_times", "hidden_postdate",
)
LOCAL_CONTEXT = {
    "explicit_workspace_or_local_context", "non_registry_dependency_spec",
    "resolved_version_present", "lockfile_context_present",
}
NODE_BUILTINS = {
    "assert", "buffer", "child_process", "cluster", "console", "constants",
    "crypto", "dgram", "diagnostics_channel", "dns", "domain", "events",
    "fs", "http", "http2", "https", "module", "net", "os", "path",
    "perf_hooks", "process", "punycode", "querystring", "readline", "repl",
    "stream", "string_decoder", "sys", "timers", "tls", "trace_events",
    "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
}
MANIFEST_NAMES = {
    "npm": {"package.json"},
    "Cargo": {"cargo.toml"},
    "PyPI": {"pyproject.toml", "setup.cfg"},
    "Go": {"go.mod"},
    "Maven": {
        "pom.xml", "build.gradle", "build.gradle.kts", "libs.versions.toml",
    },
}
LOCK_NAMES = {
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
    "cargo.lock", "go.sum", "gradle.lockfile",
}
RELEVANT_NAMES = set().union(*MANIFEST_NAMES.values(), LOCK_NAMES)
DIFF_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$")
CARGO_PAIR = re.compile(r"^\s*['\"]?([A-Za-z0-9_.-]+)['\"]?\s*=\s*(.+)$")
JSON_PAIR = re.compile(r'^\s*"([^"]+)"\s*:\s*"([^"]*)"\s*,?\s*$')
Result = tuple[str, str, str, set[str]]
CsvIndex = dict[str, dict[str, str]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in INPUT_NAMES:
        parser.add_argument(
            "--" + name.replace("_", "-"), type=Path, required=True
        )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def csv_map(path: Path) -> CsvIndex:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["episode_id"]: row for row in csv.DictReader(handle)}


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def name_tokens(value: str) -> set[str]:
    return {
        token for token in re.split(r"[^a-z0-9]+", value.lower())
        if len(token) >= 3
    }


def owned_package(repo: str, ecosystem: str, package: str) -> bool:
    owner, repo_name = repo.split("/", 1)
    if ecosystem == "npm" and package.startswith("@"):
        return normalized(package[1:].split("/", 1)[0]) == normalized(owner)
    if ecosystem in {"npm", "PyPI", "Cargo"}:
        return normalized(package) == normalized(repo_name)
    if ecosystem == "Go":
        value = package.lower().removeprefix("https://").removeprefix("http://")
        return value.startswith(f"github.com/{repo.lower()}")
    if ecosystem == "Maven" and ":" in package:
        return _owned_maven(owner, repo_name, package)
    return False


def _owned_maven(owner: str, repo_name: str, package: str) -> bool:
    group_id, artifact_id = package.split(":", 1)
    group = normalized(group_id)
    owner_tokens = name_tokens(owner)
    group_tokens = name_tokens(group_id)
    repo_tokens = name_tokens(repo_name)
    artifact_tokens = name_tokens(artifact_id)
    return (
        normalized(owner) in group
        or normalized(repo_name) in group
        or bool(owner_tokens and owner_tokens <= group_tokens)
        or len(repo_tokens & artifact_tokens) >= 2
    )


def patch_text(path: str, cache: dict[str, str]) -> str:
    if not path:
        return ""
    if path not in cache:
        cache[path] = Path(path).read_text(encoding="utf-8", errors="replace")
    return cache[path]


@lru_cache(maxsize=32)
def relevant_patch_lines(text: str) -> tuple[tuple[str, str, bool], ...]:
    current = ""
    relevant = False
    rows: list[tuple[str, str, bool]] = []
    for line in text.splitlines():
        current, relevant, parsed = _parse_patch_line(line, current, relevant)
        if parsed is not None:
            rows.append(parsed)
    return tuple(rows)


def _parse_patch_line(
    line: str,
    current: str,
    relevant: bool,
) -> tuple[str, bool, tuple[str, str, bool] | None]:
    header = DIFF_HEADER.match(line)
    if header:
        current = header.group(2).replace("\\", "/")
        name = Path(current).name.lower()
        relevant = name in RELEVANT_NAMES or name.startswith("requirements")
        return current, relevant, None
    if not relevant or line.startswith(("diff --git ", "--- ", "+++ ", "@@")):
        return current, relevant, None
    content = line[1:] if line[:1] in {"+", "-", " "} else line
    added = line.startswith("+") and not line.startswith("+++")
    return current, relevant, (current, content, added)


def matching_lines(
    rows: tuple[tuple[str, str, bool], ...],
    ecosystem: str,
    packages: set[str],
) -> tuple[str, str, str]:
    names = MANIFEST_NAMES.get(ecosystem, set())
    manifest: list[str] = []
    locks: list[str] = []
    package_json: list[str] = []
    for path, line, added in rows:
        name = Path(path).name.lower()
        hit = any(pkg.lower() in line.lower() for pkg in packages if pkg)
        if name == "package.json":
            package_json.append(line)
        if added and hit and _is_manifest_name(name, names, ecosystem):
            manifest.append(line)
        if added and hit and name in LOCK_NAMES:
            locks.append(line)
    return "\n".join(manifest), "\n".join(locks), "\n".join(package_json)


def _is_manifest_name(name: str, names: set[str], ecosystem: str) -> bool:
    return name in names or (
        ecosystem == "PyPI" and name.startswith("requirements")
    )


def dependency_manifest_lines(
    rows: tuple[tuple[str, str, bool], ...],
    ecosystem: str,
    package: str,
    effective: str,
) -> str:
    names = {package.lower(), effective.lower()} - {""}
    matched = [
        line for path, line, added in rows
        if added and _dependency_line_matches(
            Path(path).name.lower(), line, ecosystem, names
        )
    ]
    return "\n".join(matched)


def _dependency_line_matches(
    filename: str,
    line: str,
    ecosystem: str,
    names: set[str],
) -> bool:
    if ecosystem == "Cargo" and filename == "cargo.toml":
        pair = CARGO_PAIR.match(line)
        return bool(pair and cargo_pair_matches(pair.group(1), pair.group(2), names))
    if ecosystem == "npm" and filename == "package.json":
        pair = JSON_PAIR.match(line)
        return bool(pair and pair.group(1).lower() in names)
    if filename in MANIFEST_NAMES.get(ecosystem, set()):
        return any(name in line.lower() for name in names)
    return ecosystem == "PyPI" and filename.startswith("requirements") and any(
        name in line.lower() for name in names
    )


def cargo_pair_matches(key: str, value: str, names: set[str]) -> bool:
    normalized_names = {name.replace("_", "-") for name in names}
    if key.lower().replace("_", "-") in normalized_names:
        return True
    alias = re.search(r"\bpackage\s*=\s*['\"]([^'\"]+)['\"]", value)
    return bool(alias and alias.group(1).lower() in names)


def semantic_patch_flags(
    case: dict[str, str],
    text: str,
    effective_package: str = "",
) -> set[str]:
    rows = relevant_patch_lines(text)
    packages = {case["package_name"], effective_package}
    manifest = dependency_manifest_lines(
        rows, case["ecosystem"], case["package_name"], effective_package
    )
    _, locks, package_json = matching_lines(rows, case["ecosystem"], packages)
    flags = _base_semantic_flags(case, manifest, locks, package_json)
    return _ecosystem_nonregistry_flags(case, manifest, flags)


def _base_semantic_flags(
    case: dict[str, str],
    manifest: str,
    locks: str,
    package_json: str,
) -> set[str]:
    flags: set[str] = set()
    if case["ecosystem"] == "Cargo" and re.search(
        r"\b(path|registry|git)\s*=", manifest
    ):
        flags.add("cargo_nonpublic_spec")
    if re.search(r"(?i)(workspace:|file:|link:|git\+)", manifest):
        flags.add("workspace_or_nonregistry_spec")
    if re.search(r"(?i)(sha512-|checksum\s*=|h1:)", locks):
        flags.add("lock_integrity")
    if case["ecosystem"] == "npm" and _package_defined(case, package_json):
        flags.add("package_defined_in_patch")
    return flags


def _package_defined(case: dict[str, str], package_json: str) -> bool:
    package = re.escape(case["package_name"])
    pattern = r'["\']name["\']\s*:\s*["\']' + package + r'["\']'
    return bool(re.search(pattern, package_json, re.I))


def _ecosystem_nonregistry_flags(
    case: dict[str, str],
    manifest: str,
    flags: set[str],
) -> set[str]:
    if case["ecosystem"] == "npm" and re.search(
        r"(?i)(workspace:|file:|link:|git\+|https?://)", manifest
    ):
        flags.add("workspace_or_nonregistry_spec")
    if case["ecosystem"] == "PyPI" and re.search(
        r"(?i)\s@\s*(git\+|https?://|file:)", manifest
    ):
        flags.add("workspace_or_nonregistry_spec")
    return flags


def not_result(basis: str, flags: set[str]) -> Result:
    return "not_hallucination", "high", basis, flags


def indeterminate(basis: str, flags: set[str]) -> Result:
    return "indeterminate", "low", basis, flags


def temporal_label(
    case: dict[str, str],
    queue: dict[str, str],
    fresh: dict[str, str] | None,
    npm: dict[str, str] | None,
    python: dict[str, str] | None,
    text: str,
) -> Result:
    flags = set(json.loads(queue["exclusion_flags_json"]))
    excluded = _temporal_exclusion(queue, python, flags)
    if excluded is not None:
        return excluded
    if queue["strong_review_candidate"] != "true" or fresh is None:
        return indeterminate(
            "weak or import-only temporal signal after conservative exclusions",
            flags,
        )
    decision, seconds = _temporal_decision(fresh, npm)
    patch_flags = semantic_patch_flags(
        case, text, fresh["effective_package_name"]
    )
    flags |= patch_flags
    if patch_flags:
        return not_result(
            "path/workspace/custom-registry/lock or package-definition evidence",
            flags,
        )
    return _temporal_registry_result(decision, seconds, flags)


def _temporal_exclusion(
    queue: dict[str, str],
    python: dict[str, str] | None,
    flags: set[str],
) -> Result | None:
    if queue["parent_state"] == "package_and_query_preexisting":
        return not_result(
            "same package and query preexisted in first parent",
            flags | {"parent_preexisting"},
        )
    strong_not = {
        "local_workspace_or_mapping_evidence", "resolved_or_lockfile_evidence",
        "not_published_after_committer", "maven_official_query_match",
        "owned_package_release_workflow_possible",
    }
    if flags & strong_not:
        return not_result(
            "systematic local, resolved, timing, or official-registry explanation",
            flags,
        )
    if python and _is_stdlib_only(python):
        return not_result(
            "Python standard-library import, not an external dependency",
            flags | {"python_stdlib"},
        )
    return None


def _is_stdlib_only(python: Mapping[str, str]) -> bool:
    return (
        int(python["manifest_event_count"]) == 0
        and python["mapping_state"] == "stdlib_import_present"
    )


def _temporal_decision(
    fresh: dict[str, str],
    npm: dict[str, str] | None,
) -> tuple[str, str]:
    if npm is None:
        return (
            fresh["fresh_decision"],
            fresh["fresh_publish_after_committer_seconds"],
        )
    decision = {
        "semver_no_matching_version": "fresh_no_matching_version",
        "semver_matching_exists_before_or_at_committer":
            "fresh_matching_exists_before_or_at_committer",
        "semver_matching_published_after_committer":
            "fresh_matching_published_after_committer",
    }.get(npm["semver_decision"], npm["semver_decision"])
    return decision, npm["semver_publish_after_committer_seconds"]


def _temporal_registry_result(
    decision: str,
    seconds: str,
    flags: set[str],
) -> Result:
    if decision == "fresh_matching_exists_before_or_at_committer":
        return not_result(
            "fresh official registry match predates committer time",
            flags | {"fresh_before_committer"},
        )
    if decision == "fresh_no_matching_version":
        return (
            "confirmed_hallucination", "high",
            "versioned manifest change has no satisfying version in fresh public registry history",
            flags | {"fresh_no_matching_version"},
        )
    if decision == "fresh_matching_published_after_committer":
        return _postdate_result(seconds, flags)
    return indeterminate(
        "official endpoint or publication-time evidence remains incomplete",
        flags | {decision},
    )


def _postdate_result(seconds: str, flags: set[str]) -> Result:
    if seconds and int(seconds) > 86400:
        return (
            "confirmed_hallucination", "high",
            "first satisfying public version was published more than 24h after committer time",
            flags | {"fresh_postdate_gt_24h"},
        )
    return (
        "probable_hallucination", "medium",
        "first satisfying public version was published within 24h after committer time",
        flags | {"fresh_postdate_le_24h"},
    )


def current_label(
    case: dict[str, str], evidence: dict[str, str], context: dict[str, str],
    python: dict[str, str] | None, python_alt: dict[str, str] | None,
    current: dict[str, str] | None, npm: dict[str, str] | None,
    corrected: dict[str, str] | None, maven: dict[str, str] | None,
    maven_time: dict[str, str] | None, hidden: dict[str, str] | None,
    text: str,
) -> Result:
    del evidence
    checks = (
        lambda: _context_result(context),
        lambda: _python_current_result(python, python_alt),
        lambda: _ecosystem_current_result(case),
        lambda: _corrected_current_result(case, corrected, hidden),
        lambda: _maven_time_result(case, maven_time, hidden),
        lambda: _fresh_current_result(case, current, npm, text),
        lambda: _maven_current_result(case, maven),
    )
    for check in checks:
        result = check()
        if result is not None:
            return result
    return indeterminate(
        "present-day absence, import mapping, private registry, deletion, or coverage remains unresolved",
        set(),
    )


def _context_result(context: dict[str, str]) -> Result | None:
    context_flags = set(json.loads(context["context_hints_json"]))
    flags = context_flags & LOCAL_CONTEXT
    if not flags:
        return None
    return not_result(
        "same-commit local/workspace/non-registry or resolved-lock evidence",
        flags,
    )


def _python_current_result(
    python: dict[str, str] | None,
    alternate: dict[str, str] | None,
) -> Result | None:
    if python is None:
        return None
    if _is_stdlib_only(python):
        return not_result("Python standard-library import", {"python_stdlib"})
    if python["mapping_state"] == "candidate_distribution_provides_import":
        return not_result(
            "wheel evidence proves the candidate distribution provides the import",
            {"python_wheel_proof"},
        )
    if alternate and alternate[
        "verification_state"
    ] == "same_commit_alternate_manifest_match":
        return not_result(
            "same-commit manifest verifies alternate Python distribution",
            {"python_alternate_manifest"},
        )
    return None


def _ecosystem_current_result(case: dict[str, str]) -> Result | None:
    package = case["package_name"]
    if case["ecosystem"] == "Go":
        first = package.split("/", 1)[0]
        local = (
            "/internal/" in f"/{package.lower()}/"
            or "." not in first
            or owned_package(case["repo"], "Go", package)
        )
        if local:
            return not_result(
                "Go subpackage/internal/local module was queried as a registry module",
                {"go_module_granularity"},
            )
    if case["ecosystem"] == "npm":
        root = package.lower().removeprefix("node:").split("/", 1)[0]
        if root in NODE_BUILTINS:
            return not_result("Node built-in module", {"node_builtin"})
    return _cargo_repository_result(case, package)


def _cargo_repository_result(
    case: dict[str, str],
    package: str,
) -> Result | None:
    owned = owned_package(case["repo"], "Cargo", package)
    if case["ecosystem"] == "Cargo" and case["query_kind"] == "name_only" and owned:
        return not_result(
            "Rust crate matches repository package",
            {"cargo_repository_crate"},
        )
    return None


def _corrected_current_result(
    case: dict[str, str],
    corrected: dict[str, str] | None,
    hidden: dict[str, str] | None,
) -> Result | None:
    if corrected is None:
        return None
    decision = corrected["corrected_temporal_decision"]
    if decision == "corrected_match_exists_before_or_at_author":
        return not_result(
            "fresh official registry match predates author time",
            {"corrected_registry_match"},
        )
    if decision != "corrected_match_published_after_author":
        return None
    if _predates_committer(hidden):
        return not_result(
            "corrected match predates committer time",
            {"hidden_committer_correction"},
        )
    if owned_package(case["repo"], case["ecosystem"], case["package_name"]):
        return not_result(
            "project's own package release train",
            {"owned_release_workflow"},
        )
    return None


def _predates_committer(hidden: dict[str, str] | None) -> bool:
    return bool(
        hidden
        and hidden["publish_after_committer_seconds"]
        and int(hidden["publish_after_committer_seconds"]) <= 0
    )


def _maven_time_result(
    case: dict[str, str],
    maven_time: dict[str, str] | None,
    hidden: dict[str, str] | None,
) -> Result | None:
    if maven_time is None:
        return None
    decision = maven_time["corrected_temporal_decision"]
    if decision == "maven_exact_exists_before_or_at_author":
        return not_result(
            "official Maven artifact predates author time",
            {"maven_artifact_before_author"},
        )
    if decision == "maven_exact_published_after_author":
        return _maven_postdate_result(case, hidden)
    if decision == "maven_name_only_exists_time_not_comparable":
        return indeterminate(
            "Maven coordinate exists but name-only signal has no version time",
            {"maven_name_exists"},
        )
    return None


def _maven_postdate_result(
    case: dict[str, str],
    hidden: dict[str, str] | None,
) -> Result:
    if _predates_committer(hidden):
        return not_result(
            "Maven artifact predates committer time",
            {"hidden_committer_correction"},
        )
    if owned_package(case["repo"], "Maven", case["package_name"]):
        return not_result(
            "project's own Maven release train",
            {"owned_release_workflow"},
        )
    seconds = hidden["publish_after_committer_seconds"] if hidden else ""
    if seconds and int(seconds) > 86400:
        return (
            "confirmed_hallucination", "high",
            "Maven exact version published more than 24h after committer time",
            {"hidden_maven_postdate_gt_24h"},
        )
    return indeterminate(
        "Maven version postdates author but causal timing remains close",
        {"hidden_maven_postdate"},
    )


def _fresh_current_result(
    case: dict[str, str],
    current: dict[str, str] | None,
    npm: dict[str, str] | None,
    text: str,
) -> Result | None:
    if current is None:
        return None
    patch_flags = semantic_patch_flags(
        case, text, current["effective_package_name"]
    )
    if current["patch_nonpublic_spec_hint"] == "true" or patch_flags:
        return not_result(
            "patch contains path/workspace/custom-registry/lock/package-definition evidence",
            patch_flags | {"current_nonpublic_spec"},
        )
    decision = _current_decision(current, npm)
    if decision == "semver_query_match_exists_now":
        return not_result(
            "standard npm semver finds a current matching version",
            {"npm_semver_correction"},
        )
    if decision != "fresh_package_exists_query_absent_now":
        return None
    return _current_absence_result(case)


def _current_decision(
    current: dict[str, str],
    npm: dict[str, str] | None,
) -> str:
    if npm is None:
        return current["fresh_current_decision"]
    decision = npm["semver_current_decision"]
    if decision == "semver_package_exists_query_absent_now":
        return "fresh_package_exists_query_absent_now"
    return decision


def _current_absence_result(case: dict[str, str]) -> Result | None:
    package = case["package_name"]
    if "+" in case["query_value"] or package.startswith("com.unity."):
        return indeterminate(
            "version may come from an alternate package index",
            {"alternate_index_possible"},
        )
    if owned_package(case["repo"], case["ecosystem"], package):
        return None
    return (
        "probable_hallucination", "medium",
        "package exists but fresh official registry has no requested version/range",
        {"current_version_absent"},
    )


def _maven_current_result(
    case: dict[str, str],
    maven: dict[str, str] | None,
) -> Result | None:
    if maven is None:
        return None
    absent = (
        maven["official_requery_class"]
        == "official_package_exists_query_absent_or_unparsed"
    )
    dynamic = (
        "SNAPSHOT" in case["query_value"].upper()
        or "${" in case["query_value"]
    )
    if absent and not dynamic:
        return (
            "probable_hallucination", "medium",
            "Maven coordinate exists but requested version is absent from Central and Google",
            {"maven_version_absent"},
        )
    return None


def load_inputs(
    args: argparse.Namespace,
) -> tuple[CsvIndex, CsvIndex, CsvIndex, dict[str, CsvIndex]]:
    candidates = csv_map(args.candidate_csv)
    evidence = csv_map(args.evidence_csv)
    context = csv_map(args.context_csv)
    maps = {
        "python": csv_map(args.python_mapping),
        "python_alt": csv_map(args.python_alternate),
        "queue": csv_map(args.temporal_queue),
        "fresh": csv_map(args.temporal_requery),
        "temporal_npm": csv_map(args.temporal_npm_semver),
        "current": csv_map(args.current_requery),
        "current_npm": csv_map(args.current_npm_semver),
        "corrected": csv_map(args.current_temporal),
        "maven": csv_map(args.maven_requery),
        "maven_time": csv_map(args.maven_times),
        "hidden": csv_map(args.hidden_postdate),
    }
    return candidates, evidence, context, maps


def build_rows(
    candidates: CsvIndex,
    evidence: CsvIndex,
    context: CsvIndex,
    maps: dict[str, CsvIndex],
) -> tuple[list[dict[str, str]], Counter[str], int]:
    cache: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    for episode_id, case in candidates.items():
        source = evidence[episode_id]
        result = _adjudicate_case(
            episode_id, case, source, context[episode_id], maps, cache
        )
        counts[result[0]] += 1
        rows.append(_output_row(episode_id, case, source, result))
    return rows, counts, len(cache)


def _adjudicate_case(
    episode_id: str,
    case: dict[str, str],
    source: dict[str, str],
    context: dict[str, str],
    maps: dict[str, CsvIndex],
    cache: dict[str, str],
) -> Result:
    needs_patch = episode_id in maps["fresh"] or episode_id in maps["current"]
    text = patch_text(source["patch_path"], cache) if needs_patch else ""
    temporal = case["registry_status"] in {
        "published_after_author_date",
        "no_matching_version_before_author_date",
    }
    if temporal:
        return temporal_label(
            case, maps["queue"][episode_id], maps["fresh"].get(episode_id),
            maps["temporal_npm"].get(episode_id),
            maps["python"].get(episode_id), text,
        )
    return current_label(
        case, source, context, maps["python"].get(episode_id),
        maps["python_alt"].get(episode_id), maps["current"].get(episode_id),
        maps["current_npm"].get(episode_id), maps["corrected"].get(episode_id),
        maps["maven"].get(episode_id), maps["maven_time"].get(episode_id),
        maps["hidden"].get(episode_id), text,
    )


def _output_row(
    episode_id: str,
    case: dict[str, str],
    source: dict[str, str],
    result: Result,
) -> dict[str, str]:
    label, confidence, basis, flags = result
    return {
        "case_id": case["case_id"], "episode_id": episode_id,
        "languages_json": case["languages_json"], "repo": case["repo"],
        "sha": case["sha"], "agent": case["agent"],
        "ecosystem": case["ecosystem"], "package_name": case["package_name"],
        "query_kind": case["query_kind"], "query_value": case["query_value"],
        "original_registry_status": case["registry_status"],
        "final_label": label, "confidence": confidence,
        "adjudication_basis": basis,
        "evidence_flags_json": json.dumps(sorted(flags), ensure_ascii=False),
        "patch_path": source["patch_path"],
    }


def write_atomic(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    args = parse_args()
    candidates, evidence, context, maps = load_inputs(args)
    rows, counts, patches_read = build_rows(
        candidates, evidence, context, maps
    )
    write_atomic(args.output, rows)
    summary = {
        "cases": len(rows),
        "labels": counts,
        "patches_read": patches_read,
    }
    print(json.dumps(summary, ensure_ascii=False, default=dict, indent=2))


if __name__ == "__main__":
    main()


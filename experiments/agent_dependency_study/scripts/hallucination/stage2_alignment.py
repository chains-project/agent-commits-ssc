"""Pure offline normalization and alignment rules for RQ2 v2 Stage 2.

[IN]: Validated Stage 1 event rows from events.csv.
[OUT]: Query plans and conservative import-to-manifest selections.
[POS]: Pure Stage 2 policy module; it does not read files, write tables, fetch
dependency trees, or access package registries.
[SYNC]: Keep stage2_episode_linker.py, Stage 2/4 tests, the v2 schema oracle,
and RQ2_PHASE4_CORE_IMPLEMENTATION_DESIGN_2026-08-03_ZH.md synchronized.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, Mapping, Sequence

from pypi_requirement import without_environment_marker
from stage2_version_repairs import normalize_source_spec,maven_literal


LOCAL_PREFIXES = (
    "file:", "link:", "workspace:", "path:", "git:", "git+", "github:",
)
EXACT_VERSION = re.compile(r"^v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class QueryPlan:
    declared_identity: str
    package_name: str
    query_kind: str
    query_value: str
    resolution_source: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManifestSelection:
    events: tuple[Mapping[str, str], ...]
    link_method: str
    alignment_status: str
    reasons: tuple[str, ...] = ()


def split_reasons(value: str) -> tuple[str, ...]:
    return tuple(sorted({item for item in value.split("|") if item}))


def normalize_package(ecosystem: str, package: str) -> str:
    value = package.strip()
    if ecosystem == "PyPI":
        return re.sub(r"[-_.]+", "-", value).lower()
    if ecosystem in {"npm", "Cargo"}:
        return value.lower()
    return value


def plan_manifest(event: Mapping[str, str]) -> QueryPlan:
    ecosystem = event["ecosystem"]
    declared = normalize_package(ecosystem, event["package_candidate"])
    package, spec, source, extra = _npm_alias(event, declared)
    spec = normalize_source_spec(event,spec)
    reasons = set(split_reasons(event.get("reason_codes", ""))) | set(extra)
    kind, value, classified = classify_version(ecosystem, spec)
    reasons.update(classified)
    if event.get("event_status") == "unresolved_gradle_alias":
        kind, value, package = "unresolved", "", ""
    return QueryPlan(declared, package, kind, value, source, tuple(sorted(reasons)))


def _npm_alias(
    event: Mapping[str, str], declared: str
) -> tuple[str, str, str, tuple[str, ...]]:
    spec = event.get("version_spec", "").strip()
    if event.get("ecosystem") != "npm" or not spec.startswith("npm:"):
        return declared, spec, "direct_manifest", ()
    payload = spec.removeprefix("npm:")
    alias = _split_explicit_npm_alias(payload)
    if alias is None:
        return declared, payload, "npm_protocol_declared", ("npm_protocol",)
    target, version = alias
    return normalize_package("npm", target), version, "npm_alias_target", ("npm_alias",)


def _split_explicit_npm_alias(value: str) -> tuple[str, str] | None:
    position = value.rfind("@")
    if position <= 0:
        return None
    if value.startswith("@") and position < value.find("/"):
        return None
    return value[:position], value[position + 1:]


def classify_version(
    ecosystem: str, version_spec: str
) -> tuple[str, str, tuple[str, ...]]:
    spec = version_spec.strip()
    if ecosystem == "PyPI":
        spec = without_environment_marker(spec)
    spec = spec.strip('"').strip("'")
    if not spec:
        return "name_only", "", ("version_unresolved",)
    if is_private_or_local(spec):
        return "excluded", "", ("private_or_local",)
    if "${" in spec or _is_gradle_alias(spec):
        return "name_only", "", ("version_unresolved",)
    if ecosystem == "PyPI":
        return _classify_pypi(spec)
    if ecosystem == "Cargo":
        return _classify_cargo(spec)
    if ecosystem == "Go":
        return ("exact", spec, ()) if EXACT_VERSION.fullmatch(spec) else ("range", spec, ())
    if ecosystem == "Maven" and maven_literal(spec):
        return "exact", spec, ()
    return _classify_general(spec)


def is_private_or_local(spec: str) -> bool:
    lowered = spec.lower()
    if lowered.startswith(LOCAL_PREFIXES):
        return True
    return lowered.startswith(("../", "./", "/")) or "{ path" in lowered


def _is_gradle_alias(spec: str) -> bool:
    return spec.startswith("libs.") and not EXACT_VERSION.fullmatch(spec)


def _classify_pypi(spec: str) -> tuple[str, str, tuple[str, ...]]:
    exact = re.fullmatch(r"={2,3}\s*([^,;\s]+)", spec)
    if exact:
        if '*' in exact.group(1) and not spec.startswith('==='):
            return "range", spec, ()
        return "exact", exact.group(1), ()
    if EXACT_VERSION.fullmatch(spec):
        return "exact", spec, ()
    return "range", spec, ()


def _classify_cargo(spec: str) -> tuple[str, str, tuple[str, ...]]:
    if spec.startswith("=") and EXACT_VERSION.fullmatch(spec[1:].strip()):
        return "exact", spec[1:].strip(), ()
    return "range", spec, ()


def _classify_general(spec: str) -> tuple[str, str, tuple[str, ...]]:
    if EXACT_VERSION.fullmatch(spec):
        return "exact", spec, ()
    return "range", spec, ()


def select_manifests(
    import_event: Mapping[str, str], manifests: Sequence[Mapping[str, str]]
) -> ManifestSelection:
    same_ecosystem = [row for row in manifests if row["ecosystem"] == import_event["ecosystem"]]
    if import_event["ecosystem"] == "Maven" and not import_event["package_candidate"]:
        return _select_java(import_event, same_ecosystem)
    matching = [row for row in same_ecosystem if identities_match(import_event, row)]
    nearest = nearest_module_events(import_event["path"], matching)
    status = _matched_alignment_status(import_event, nearest)
    return ManifestSelection(tuple(nearest), "identity_match", status)


def _matched_alignment_status(import_event, manifests) -> str:
    ecosystem = import_event["ecosystem"]
    if ecosystem == "npm" and any(row["version_spec"].startswith("npm:") for row in manifests):
        return "matched_manifest_alias"
    event_status = import_event.get("event_status", "")
    if ecosystem == "PyPI" and event_status == "alias_mapping":
        return "matched_manifest_alias"
    if ecosystem == "PyPI" and import_event["raw_target"].lower() != import_event["package_candidate"].lower():
        return "matched_manifest_normalized"
    return "matched_manifest"


def identities_match(
    import_event: Mapping[str, str], manifest_event: Mapping[str, str]
) -> bool:
    ecosystem = import_event["ecosystem"]
    imported = normalize_package(ecosystem, import_event["package_candidate"])
    declared = normalize_package(ecosystem, manifest_event["package_candidate"])
    if ecosystem == "Go":
        return imported == declared or imported.startswith(declared + "/")
    return bool(imported) and imported == declared


def nearest_module_events(
    source_path: str, events: Iterable[Mapping[str, str]]
) -> list[Mapping[str, str]]:
    rows = list(events)
    ancestors = [(module_depth(source_path, row["path"]), row) for row in rows]
    valid = [(depth, row) for depth, row in ancestors if depth >= 0]
    if not valid:
        return rows
    deepest = max(depth for depth, _row in valid)
    return [row for depth, row in valid if depth == deepest]


def module_depth(source_path: str, manifest_path: str) -> int:
    source_parts = PurePosixPath(source_path).parts[:-1]
    manifest_parts = PurePosixPath(manifest_path).parts[:-1]
    if source_parts[:len(manifest_parts)] != manifest_parts:
        return -1
    return len(manifest_parts)


def _select_java(
    import_event: Mapping[str, str], manifests: Sequence[Mapping[str, str]]
) -> ManifestSelection:
    nearest = nearest_module_events(import_event["path"], manifests)
    packages = {normalize_package("Maven", row["package_candidate"]) for row in nearest}
    packages.discard("")
    if len(packages) == 1:
        return ManifestSelection(
            tuple(row for row in nearest if row["package_candidate"]),
            "unique_module_coordinate", "matched_manifest_unique_coordinate",
            ("resolved_unique_module_coordinate",),
        )
    reasons = ("multiple_package_candidates",) if len(packages) > 1 else ()
    return ManifestSelection((), "unresolved_mapping", "mapping_uncertain", reasons)

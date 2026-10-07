"""Evaluate RQ2 v2 registry evidence at commit time without network access.

[IN]: Stage 2 episode rows, commit author dates, and normalized registry
package snapshots containing RFC3339 timestamps at arbitrary fractional precision.
[OUT]: Independent registry decisions for exact, name-only, and range queries.
[POS]: Pure Stage 3 policy module; it performs no I/O and no registry requests.
[SYNC]: Keep stage3_apply.py, Stage 3 tests, the v2 oracle, and the Chinese
core design synchronized with status or range-semantics changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Sequence

import packaging
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


PACKAGING_VERSION = packaging.__version__
Comparator = tuple[str, Version]
Matcher = Callable[[str], Optional[bool]]
RFC3339_OVERPRECISE_FRACTION = re.compile(
    r"(?P<prefix>T\d{2}:\d{2}:\d{2}\.\d{6})\d+"
    r"(?P<zone>Z|[+-]\d{2}:\d{2})$"
)


@dataclass(frozen=True)
class Release:
    version: str
    published_at: datetime | None


@dataclass(frozen=True)
class RegistryDecision:
    status: str
    reasons: tuple[str, ...]
    matched_versions: tuple[str, ...] = ()


def registry_query_key(ecosystem: str, package_name: str) -> str:
    return f"registry|{ecosystem}|{package_name}"


def parse_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = RFC3339_OVERPRECISE_FRACTION.sub(
        r"\g<prefix>\g<zone>", text
    )
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def evaluate_registry(
    episode: Mapping[str, str], author_date: str, evidence: Mapping[str, object]
) -> RegistryDecision:
    lookup = str(evidence.get("lookup_status", ""))
    if lookup == "not_found":
        return _current_absence("registry_package_not_found_at_query")
    if lookup != "ok":
        reason = f"registry_{lookup or 'lookup_failed'}"
        return RegistryDecision("cannot_compare", (reason,))
    releases = _release_rows(evidence.get("versions", []))
    author = parse_time(author_date)
    if author is None:
        return RegistryDecision("cannot_compare", ("author_date_missing",))
    kind = episode["query_kind"]
    if kind == "name_only":
        return _decide_releases(releases, author)
    if kind == "exact":
        if not releases:
            return _current_absence("registry_release_history_empty")
        selected = [row for row in releases if exact_equal(row.version, episode["query_value"])]
        return _exact_decision(selected, author)
    if kind == "range":
        return _range_decision(episode["ecosystem"], episode["query_value"], releases, author)
    return RegistryDecision("cannot_compare", ("query_kind_not_registry_eligible",))


def _release_rows(value: object) -> list[Release]:
    if not isinstance(value, list):
        raise ValueError("registry versions must be a list")
    rows = []
    for item in value:
        if not isinstance(item, Mapping) or not str(item.get("version", "")).strip():
            raise ValueError("registry version row requires version")
        rows.append(Release(str(item["version"]).strip(), parse_time(str(item.get("published_at", "")))))
    return rows


def exact_equal(left: str, right: str) -> bool:
    return _exact_text(left) == _exact_text(right)


def _exact_text(value: str) -> str:
    text = value.strip().lower()
    return text[1:] if re.fullmatch(r"v\d.*", text) else text


def _exact_decision(releases: Sequence[Release], author: datetime) -> RegistryDecision:
    if not releases:
        return _current_absence("registry_requested_version_absent")
    decision = _decide_releases(releases, author)
    return RegistryDecision(decision.status, decision.reasons, tuple(row.version for row in releases))


def _decide_releases(releases: Sequence[Release], author: datetime) -> RegistryDecision:
    if not releases:
        return _current_absence("registry_release_history_empty")
    before = [row for row in releases if row.published_at and row.published_at <= author]
    missing = [row for row in releases if row.published_at is None]
    if before:
        return RegistryDecision("exists_before_author_date", (), tuple(row.version for row in before))
    if missing:
        return RegistryDecision("cannot_compare", ("registry_publish_time_missing",))
    return RegistryDecision(
        "published_after_author_date", ("history_validation_required",),
        tuple(row.version for row in releases),
    )


def _current_absence(reason: str) -> RegistryDecision:
    return RegistryDecision(
        "current_absent_unknown",
        ("unknown_deleted_or_unpublished_possible", reason),
    )


def _range_decision(
    ecosystem: str, spec: str, releases: Sequence[Release], author: datetime
) -> RegistryDecision:
    matcher = build_range_matcher(ecosystem, spec)
    if matcher is None:
        return RegistryDecision("cannot_compare", ("unsupported_range_syntax",))
    if not releases:
        return _current_absence("registry_release_history_empty")
    matched, unknown = _matching_releases(releases, matcher)
    if not matched and unknown:
        return RegistryDecision("cannot_compare", ("range_candidate_version_unparseable",))
    if not matched:
        return RegistryDecision(
            "no_matching_version_before_author_date", ("history_validation_required",)
        )
    return _exact_decision(matched, author)


def _matching_releases(
    releases: Sequence[Release], matcher: Matcher
) -> tuple[list[Release], int]:
    matched, unknown = [], 0
    for release in releases:
        result = matcher(release.version)
        if result is True:
            matched.append(release)
        elif result is None:
            unknown += 1
    return matched, unknown


def build_range_matcher(ecosystem: str, spec: str) -> Matcher | None:
    if ecosystem == "PyPI":
        return _pypi_matcher(spec)
    if ecosystem in {"npm", "Cargo"}:
        return _semver_matcher(spec, cargo=ecosystem == "Cargo")
    if ecosystem == "Maven":
        return _maven_matcher(spec)
    return None


def _pypi_matcher(spec: str) -> Matcher | None:
    try:
        parsed = SpecifierSet(spec)
    except InvalidSpecifier:
        return None

    def match(value: str) -> bool | None:
        try:
            return Version(value) in parsed
        except InvalidVersion:
            return None

    return match


def _semver_matcher(spec: str, *, cargo: bool) -> Matcher | None:
    branches = []
    for text in spec.split("||"):
        parsed = _semver_branch(text.strip(), cargo=cargo)
        if parsed is None:
            return None
        branches.append(parsed)
    if not branches:
        return None
    allow_prerelease = bool(re.search(r"\d-[0-9A-Za-z]", spec))

    def match(value: str) -> bool | None:
        version = _version(value)
        if version is None:
            return None
        if version.is_prerelease and not allow_prerelease:
            return False
        return _match_branches(value, branches)

    return match


def _semver_branch(text: str, *, cargo: bool) -> list[Comparator] | None:
    if not text:
        return None
    hyphen = re.fullmatch(r"\s*(\S+)\s+-\s+(\S+)\s*", text)
    if hyphen:
        return _bounded(hyphen.group(1), hyphen.group(2), True, True)
    tokens = [token for token in re.split(r"[\s,]+", text) if token]
    constraints: list[Comparator] = []
    for token in tokens:
        parsed = _semver_token(token, cargo=cargo)
        if parsed is None:
            return None
        constraints.extend(parsed)
    return constraints


def _semver_token(token: str, *, cargo: bool) -> list[Comparator] | None:
    if token in {"*", "x", "X"}:
        return []
    wildcard_parts = token.lstrip("v").replace("X", "x").split(".")
    if any(part in {"*", "x"} for part in wildcard_parts):
        return _wildcard_bounds(token)
    if token.startswith("^") or (cargo and token[0].isdigit()):
        value = token[1:] if token.startswith("^") else token
        return _caret_bounds(value)
    if token.startswith("~"):
        return _tilde_bounds(token.lstrip("~>"))
    if _partial_version(token):
        return _wildcard_bounds(token)
    match = re.fullmatch(r"(<=|>=|<|>|=)?(.+)", token)
    version = _version(match.group(2)) if match else None
    return [(match.group(1) or "=", version)] if version is not None else None


def _partial_version(value: str) -> bool:
    return bool(re.fullmatch(r"v?\d+(?:\.\d+)?", value))


def _caret_bounds(value: str) -> list[Comparator] | None:
    version = _version(value)
    if version is None:
        return None
    major, minor, patch = _release_triplet(version)
    upper = (major + 1, 0, 0) if major else ((0, minor + 1, 0) if minor else (0, 0, patch + 1))
    return [(">=", version), ("<", Version(".".join(map(str, upper))))]


def _tilde_bounds(value: str) -> list[Comparator] | None:
    version = _version(value)
    if version is None:
        return None
    parts = value.lstrip("v").split(".")
    major, minor, _patch = _release_triplet(version)
    upper = (major, minor + 1, 0) if len(parts) > 1 else (major + 1, 0, 0)
    return [(">=", version), ("<", Version(".".join(map(str, upper))))]


def _wildcard_bounds(value: str) -> list[Comparator] | None:
    cleaned = value.lstrip("v").replace("X", "x")
    parts = cleaned.split(".")
    numeric: list[int] = []
    wildcard_seen = False
    for part in parts:
        if part in {"x", "*"}:
            wildcard_seen = True
        elif wildcard_seen or not part.isdigit():
            return None
        else:
            numeric.append(int(part))
    if not numeric:
        return None
    if len(numeric) >= 3 and len(numeric) == len(parts):
        version = Version(".".join(map(str, numeric)))
        return [("=", version)]
    lower_parts = (numeric + [0, 0])[:3]
    upper = [lower_parts[0] + 1, 0, 0] if len(numeric) == 1 else [lower_parts[0], lower_parts[1] + 1, 0]
    return _bounded(".".join(map(str, lower_parts)), ".".join(map(str, upper)), True, False)


def _maven_matcher(spec: str) -> Matcher | None:
    match = re.fullmatch(r"\s*(\[|\()\s*([^,]*)\s*,\s*([^\]\)]*)\s*(\]|\))\s*", spec)
    if not match:
        return None
    lower, upper = match.group(2), match.group(3)
    if any(value and not _numeric_maven_version(value) for value in (lower, upper)):
        return None
    constraints = _bounded(lower, upper, match.group(1) == "[", match.group(4) == "]")
    if constraints is None:
        return None

    def match_version(value: str) -> bool | None:
        if not _numeric_maven_version(value):
            return None
        return _match_constraints(value, constraints)

    return match_version


def _numeric_maven_version(value: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)*", value.strip()))


def _bounded(
    lower: str, upper: str, include_lower: bool, include_upper: bool
) -> list[Comparator] | None:
    constraints: list[Comparator] = []
    for value, included, low in ((lower, include_lower, True), (upper, include_upper, False)):
        if not value:
            continue
        version = _version(value)
        if version is None:
            return None
        constraints.append(((">=" if included else ">") if low else ("<=" if included else "<"), version))
    return constraints


def _match_branches(value: str, branches: Sequence[Sequence[Comparator]]) -> bool | None:
    results = [_match_constraints(value, branch) for branch in branches]
    if True in results:
        return True
    return None if None in results else False


def _match_constraints(value: str, constraints: Sequence[Comparator]) -> bool | None:
    version = _version(value)
    if version is None:
        return None
    return all(_compare(version, operator, expected) for operator, expected in constraints)


def _compare(actual: Version, operator: str, expected: Version) -> bool:
    return {
        "=": actual == expected, ">": actual > expected, ">=": actual >= expected,
        "<": actual < expected, "<=": actual <= expected,
    }[operator]


def _version(value: str) -> Version | None:
    try:
        return Version(value.strip())
    except InvalidVersion:
        return None


def _release_triplet(version: Version) -> tuple[int, int, int]:
    return tuple((list(version.release) + [0, 0, 0])[:3])

"""Build compact dependency episodes and event links for RQ2 v2 Stage 2.

[IN]: Validated commits.csv and events.csv rows produced by Stage 1.
[OUT]: Deterministic episodes.csv and episode_event_links.csv row lists.
[POS]: Pure in-memory Stage 2 linker. It preserves event-level observations,
deduplicates dependency queries, and never accesses dependency networks.
[SYNC]: Keep rq2_stage2_alignment.py, rq2_stage_schema.py, Stage 2 tests, and
scripts/OUTPUTS.md synchronized with episode/link behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from rq2_stage2_alignment import (
    QueryPlan,
    module_depth,
    normalize_package,
    plan_manifest,
    select_manifests,
    split_reasons,
)
from rq2_stage_schema import (
    SCHEMA_VERSION,
    canonical_episode_key,
    make_episode_id,
    validate_row,
)


@dataclass
class EpisodeDraft:
    commit_id: str
    ecosystem: str
    identity_key: str
    package_name: str
    alignment_status: str
    query_kind: str
    query_value: str
    resolution_source: str
    reasons: set[str] = field(default_factory=set)
    links: dict[str, str] = field(default_factory=dict)
    event_types: set[str] = field(default_factory=set)


def build_episode_tables(
    commit_rows: Iterable[Mapping[str, str]],
    event_rows: Iterable[Mapping[str, str]],
    context_rows: Iterable[Mapping[str, str]] = (),
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    commits = list(commit_rows)
    events = list(event_rows)
    contexts = _group_context(context_rows)
    _validate_inputs(commits, events)
    grouped = _group_events(events)
    drafts: dict[tuple[str, ...], EpisodeDraft] = {}
    for commit in sorted(commits, key=lambda row: row["commit_id"]):
        key = (commit["repo"].lower(), commit["sha"].lower())
        _build_commit(grouped.get(commit["commit_id"], []), drafts, contexts.get(key, []))
    episodes, links = _materialize(drafts)
    _validate_outputs(events, episodes, links)
    return episodes, links


def _group_context(
    rows: Iterable[Mapping[str, str]],
) -> dict[tuple[str, str], list[Mapping[str, str]]]:
    grouped: dict[tuple[str, str], list[Mapping[str, str]]] = {}
    for row in rows:
        key = (row.get("repo", "").lower(), row.get("sha", "").lower())
        grouped.setdefault(key, []).append(row)
    return grouped
def _validate_inputs(
    commits: list[Mapping[str, str]], events: list[Mapping[str, str]]
) -> None:
    commit_ids = set()
    for row in commits:
        validate_row("commits.csv", row)
        if row["commit_id"] in commit_ids:
            raise ValueError(f"duplicate commit_id: {row['commit_id']}")
        commit_ids.add(row["commit_id"])
    _validate_event_inputs(events, commit_ids)


def _validate_event_inputs(
    events: list[Mapping[str, str]], commit_ids: set[str]
) -> None:
    event_ids = set()
    for row in events:
        validate_row("events.csv", row)
        if row["event_id"] in event_ids:
            raise ValueError(f"duplicate event_id: {row['event_id']}")
        if row["commit_id"] not in commit_ids:
            raise ValueError(f"event references unknown commit: {row['event_id']}")
        event_ids.add(row["event_id"])


def _group_events(
    events: Iterable[Mapping[str, str]],
) -> dict[str, list[Mapping[str, str]]]:
    grouped: dict[str, list[Mapping[str, str]]] = {}
    for row in events:
        grouped.setdefault(row["commit_id"], []).append(row)
    return grouped


def _build_commit(
    events: list[Mapping[str, str]], drafts: dict[tuple[str, ...], EpisodeDraft],
    contexts: list[Mapping[str, str]],
) -> None:
    manifests = [row for row in events if row["event_type"] == "manifest_addition"]
    imports = [row for row in events if row["event_type"] == "import"]
    matched: set[str] = set()
    for event in imports:
        selection = select_manifests(event, manifests)
        context = _select_context(event, contexts)
        if selection.events and context and _context_refines(context):
            _add_matched_context(event, selection, context, drafts)
            matched.update(row["event_id"] for row in selection.events)
        elif selection.events:
            _add_matched(event, selection, drafts)
            matched.update(row["event_id"] for row in selection.events)
        elif context:
            _add_context_only(event, context, drafts)
        else:
            _add_import_only(event, manifests, selection.reasons, drafts)
    for event in manifests:
        if event["event_id"] not in matched:
            context = _select_manifest_context(event, contexts)
            _add_manifest_only(event, drafts, context)


def _select_context(event, contexts):
    rows = [row for row in contexts if row.get("ecosystem") == event["ecosystem"]]
    path_rows = [row for row in rows if row.get("source_path") == event["path"]]
    exact = [row for row in path_rows if row.get("raw_target") == event["raw_target"]]
    candidates = exact or [
        row for row in path_rows
        if row.get("import_root") in {event["raw_target"], event["package_candidate"]}
    ]
    if event["ecosystem"] == "PyPI":
        root = event["raw_target"].lstrip(".").split(".", 1)[0]
        candidates.extend(
            row for row in rows
            if row.get("source_path") == "*" and row.get("import_root") == root
        )
    if event["ecosystem"] == "Go":
        candidates.extend(_go_context_candidates(event, rows))
        key = lambda row: _go_context_priority(row, event)
    else:
        key = lambda row: _event_context_priority(row, event)
    return sorted(candidates, key=key)[0] if candidates else None


def _go_context_candidates(event, rows):
    target = event.get("package_candidate", "")
    return [
        row for row in rows
        if row.get("package_name")
        and (target == row["package_name"] or target.startswith(row["package_name"] + "/"))
        and module_depth(event["path"], row.get("source_path", "")) >= 0
    ]


def _go_context_priority(row, event) -> tuple[int, int, int, str]:
    priority, package = _context_priority(row)
    depth = module_depth(event["path"], row.get("source_path", ""))
    return priority, -depth, -len(package), package


def _event_context_priority(row, event) -> tuple[int, int, str]:
    priority, package = _context_priority(row)
    wildcard = int(row.get("source_path") != event["path"])
    return priority, wildcard, package


def _select_manifest_context(event, contexts):
    rows = [row for row in contexts if row.get("ecosystem") == event["ecosystem"]]
    rows = [row for row in rows if row.get("source_path") == event["path"]]
    package = normalize_package(event["ecosystem"], event["package_candidate"])
    if event.get("event_status") == "unresolved_gradle_alias":
        package = ""
    matching = [
        row for row in rows
        if not package or normalize_package(event["ecosystem"], row.get("package_name", "")) == package
    ]
    return sorted(matching, key=_context_priority)[0] if matching else None

def _context_priority(row) -> tuple[int, str]:
    order = {
        "first_party_excluded": 0, "matched_manifest_and_lock": 1,
        "matched_manifest": 2, "matched_existing_manifest": 3,
        "lockfile_only_observed": 4, "mapping_uncertain": 5,
    }
    return order.get(row.get("alignment_status", ""), 9), row.get("package_name", "")


def _context_refines(context) -> bool:
    return bool(context.get("resolved_version")) or context.get("alignment_status") in {
        "first_party_excluded", "matched_manifest_and_lock",
    }


def _add_matched_context(event, selection, context, drafts) -> None:
    plan = _context_plan(event, context)
    status = context.get("alignment_status") or selection.alignment_status
    draft = _draft_for(event, plan, _matched_status(plan, status), drafts)
    draft.reasons.update(selection.reasons)
    _link(draft, event, selection.link_method)
    for manifest in selection.events:
        _link(draft, manifest, selection.link_method)


def _add_context_only(event, context, drafts) -> None:
    plan = _context_plan(event, context)
    status = context.get("alignment_status") or "matched_existing_manifest"
    draft = _draft_for(event, plan, _matched_status(plan, status), drafts)
    _link(draft, event, "context_match")


def _context_plan(event, context) -> QueryPlan:
    reasons = set(split_reasons(context.get("reason_codes", "")))
    status = context.get("alignment_status", "")
    package = normalize_package(event["ecosystem"], context.get("package_name", ""))
    if status == "first_party_excluded":
        identity = package or normalize_package(event["ecosystem"], event["package_candidate"])
        return QueryPlan(identity, identity, "excluded", "", "first_party_context", tuple(sorted(reasons)))
    if status == "mapping_uncertain" or not package:
        identity = f"unresolved:{event['raw_target']}"
        reasons.add("mapping_uncertain")
        return QueryPlan(identity, "", "unresolved", "", "context_mapping", tuple(sorted(reasons)))
    version = context.get("resolved_version") or context.get("version_spec", "")
    manifest = {
        **event, "package_candidate": package, "version_spec": version,
        "event_status": "direct_manifest", "reason_codes": "|".join(sorted(reasons)),
    }
    base = plan_manifest(manifest)
    source = context.get("resolution_source") or "existing_manifest_context"
    return QueryPlan(
        base.declared_identity, base.package_name, base.query_kind,
        base.query_value, source, base.reasons,
    )
def _add_matched(event, selection, drafts) -> None:
    for manifest in selection.events:
        plan = plan_manifest(manifest)
        status = _matched_status(plan, selection.alignment_status)
        draft = _draft_for(event, plan, status, drafts)
        draft.reasons.update(selection.reasons)
        _link(draft, event, selection.link_method)
        _link(draft, manifest, selection.link_method)


def _matched_status(plan: QueryPlan, default: str) -> str:
    if plan.query_kind == "excluded":
        return default if default == "first_party_excluded" else "private_or_local_excluded"
    if plan.query_kind == "unresolved":
        return "mapping_uncertain"
    return default


def _add_import_only(event, manifests, extra_reasons, drafts) -> None:
    package = normalize_package(event["ecosystem"], event["package_candidate"])
    reasons = set(split_reasons(event.get("reason_codes", ""))) | set(extra_reasons)
    if package:
        plan = QueryPlan(package, package, "name_only", "", event["event_status"], tuple(reasons))
        status = "no_matching_manifest"
        if any(row["ecosystem"] == event["ecosystem"] for row in manifests):
            plan = _with_reason(plan, "manifest_does_not_explain_import")
    else:
        identity = f"unresolved:{event['raw_target']}"
        plan = QueryPlan(identity, "", "unresolved", "", "unresolved_mapping", tuple(reasons))
        status = "mapping_uncertain"
    draft = _draft_for(event, plan, status, drafts)
    _link(draft, event, "import_only" if package else "unresolved_mapping")


def _with_reason(plan: QueryPlan, reason: str) -> QueryPlan:
    reasons = tuple(sorted(set(plan.reasons) | {reason}))
    return QueryPlan(
        plan.declared_identity, plan.package_name, plan.query_kind,
        plan.query_value, plan.resolution_source, reasons,
    )


def _add_manifest_only(event, drafts, context=None) -> None:
    plan = _context_plan(event, context) if context else plan_manifest(event)
    status = "private_or_local_excluded" if plan.query_kind == "excluded" else "not_applicable_manifest_only"
    draft = _draft_for(event, plan, status, drafts)
    _link(draft, event, "manifest_only")


def _draft_for(event, plan, status, drafts) -> EpisodeDraft:
    identity = plan.package_name or plan.declared_identity
    canonical_key = canonical_episode_key(
        event["ecosystem"], identity, plan.query_kind, plan.query_value
    )
    key = (event["commit_id"], *canonical_key)
    if key not in drafts:
        drafts[key] = EpisodeDraft(
            event["commit_id"], event["ecosystem"], canonical_key[1],
            plan.package_name, status, canonical_key[2], canonical_key[3],
            plan.resolution_source, set(plan.reasons),
        )
    else:
        drafts[key].reasons.update(plan.reasons)
    return drafts[key]


def _link(draft: EpisodeDraft, event: Mapping[str, str], method: str) -> None:
    draft.links[event["event_id"]] = method
    draft.event_types.add(event["event_type"])


def _materialize(
    drafts: Mapping[tuple[str, ...], EpisodeDraft]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    episodes, links = [], []
    for key in sorted(drafts):
        draft = drafts[key]
        episode = _episode_row(draft)
        episodes.append(episode)
        links.extend(_link_rows(episode["episode_id"], draft.links))
    return episodes, sorted(links, key=lambda row: (row["episode_id"], row["event_id"]))


def _episode_row(draft: EpisodeDraft) -> dict[str, str]:
    episode_id = make_episode_id(
        draft.commit_id, draft.ecosystem, draft.identity_key,
        draft.query_kind, draft.query_value,
    )
    registry = _initial_registry_status(draft.query_kind)
    return {
        "episode_id": episode_id, "commit_id": draft.commit_id,
        "ecosystem": draft.ecosystem, "package_name": draft.package_name,
        "dependency_quadrant": _quadrant(draft.event_types),
        "alignment_status": draft.alignment_status,
        "query_kind": draft.query_kind, "query_value": draft.query_value,
        "resolution_source": draft.resolution_source,
        "registry_status": registry, "advisory_status": "not_queried",
        "reason_codes": "|".join(sorted(draft.reasons)), "evidence_ids": "",
        "schema_version": str(SCHEMA_VERSION),
    }


def _initial_registry_status(query_kind: str) -> str:
    if query_kind == "excluded":
        return "excluded"
    if query_kind == "unresolved":
        return "cannot_compare"
    return "not_queried"


def _quadrant(event_types: set[str]) -> str:
    if event_types == {"import", "manifest_addition"}:
        return "I+M+"
    if event_types == {"import"}:
        return "I+M-"
    if event_types == {"manifest_addition"}:
        return "I-M+"
    raise ValueError(f"invalid episode event types: {sorted(event_types)}")


def _link_rows(episode_id: str, links: Mapping[str, str]) -> list[dict[str, str]]:
    return [
        {
            "episode_id": episode_id, "event_id": event_id,
            "link_method": method, "schema_version": str(SCHEMA_VERSION),
        }
        for event_id, method in sorted(links.items())
    ]


def _validate_outputs(events, episodes, links) -> None:
    episode_ids = {row["episode_id"] for row in episodes}
    event_ids = {row["event_id"] for row in events}
    link_keys = {(row["episode_id"], row["event_id"]) for row in links}
    linked_event_ids = {row["event_id"] for row in links}
    if len(episode_ids) != len(episodes) or len(link_keys) != len(links):
        raise ValueError("duplicate Stage 2 primary key")
    if linked_event_ids != event_ids:
        raise ValueError("not every Stage 1 event is linked into Stage 2")
    for row in episodes:
        validate_row("episodes.csv", row)
    for row in links:
        validate_row("episode_event_links.csv", row)
        if row["episode_id"] not in episode_ids or row["event_id"] not in event_ids:
            raise ValueError("episode link violates referential closure")

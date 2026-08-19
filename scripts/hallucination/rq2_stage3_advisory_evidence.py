"""Normalize Stage 3 evidence and evaluate advisory lifecycle at commit time.

[IN]: Offline registry/advisory JSON records and a commit author timestamp.
[OUT]: Stable evidence metadata/content payloads and an independent advisory
decision using hydrated published/withdrawn fields.
[POS]: Stage 3 evidence boundary; it canonicalizes in memory but performs no
network access and leaves filesystem writes to rq2_stage3_apply.py.
[SYNC]: Keep rq2_stage3_apply.py, rq2_stage3_version_query.py, Stage 3 tests,
the v2 schema oracle, RQ2_STAGE3_EVIDENCE_INPUT.md, and
scripts/OUTPUTS.md synchronized.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from rq2_stage3_version_query import parse_time, registry_query_key
from rq2_stage_schema import SCHEMA_VERSION, make_evidence_id


@dataclass(frozen=True)
class AdvisoryDecision:
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceItem:
    query_key: str
    evidence_id: str
    record: Mapping[str, object]
    metadata: Mapping[str, object]
    payload_sha256: str
    payload: bytes


@dataclass(frozen=True)
class EvidenceBundle:
    by_query_key: Mapping[str, EvidenceItem]
    items: tuple[EvidenceItem, ...]
    payloads: Mapping[str, bytes]


def advisory_query_key(
    ecosystem: str, package_name: str, query_kind: str, query_value: str
) -> str:
    return f"advisory|{ecosystem}|{package_name}|{query_kind}|{query_value}"


def evidence_query_key(record: Mapping[str, object]) -> str:
    kind = str(record.get("evidence_type", ""))
    ecosystem = str(record.get("ecosystem", ""))
    package = str(record.get("package_name", ""))
    if kind == "registry":
        return registry_query_key(ecosystem, package)
    if kind == "advisory":
        return advisory_query_key(
            ecosystem, package, str(record.get("query_kind", "")),
            str(record.get("query_value", "")),
        )
    raise ValueError(f"invalid evidence_type: {kind!r}")


def build_evidence_bundle(records: Sequence[Mapping[str, object]]) -> EvidenceBundle:
    by_key: dict[str, EvidenceItem] = {}
    items: dict[str, EvidenceItem] = {}
    payloads: dict[str, bytes] = {}
    for record in records:
        _validate_record(record)
        item = _evidence_item(record)
        previous = by_key.get(item.query_key)
        if previous and previous.payload_sha256 != item.payload_sha256:
            raise ValueError(f"conflicting evidence for query: {item.query_key}")
        by_key[item.query_key] = item
        items[item.evidence_id] = item
        payloads[item.payload_sha256] = item.payload
    ordered = tuple(items[key] for key in sorted(items))
    return EvidenceBundle(by_key, ordered, payloads)


def _validate_record(record: Mapping[str, object]) -> None:
    required = ("evidence_type", "source", "ecosystem", "package_name", "lookup_status", "queried_at")
    missing = [field for field in required if not str(record.get(field, "")).strip()]
    if missing:
        raise ValueError(f"evidence record missing fields: {missing}")
    parse_time(str(record["queried_at"]))
    if record["evidence_type"] == "registry" and not isinstance(record.get("versions", []), list):
        raise ValueError("registry evidence versions must be a list")
    if record["evidence_type"] == "advisory":
        _validate_advisory_record(record)


def _validate_advisory_record(record: Mapping[str, object]) -> None:
    if str(record.get("query_kind", "")) not in {"exact", "name_only", "range"}:
        raise ValueError("advisory evidence requires a query_kind")
    if not isinstance(record.get("advisories", []), list):
        raise ValueError("advisory evidence advisories must be a list")


def _evidence_item(record: Mapping[str, object]) -> EvidenceItem:
    payload = json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    query_key = evidence_query_key(record)
    evidence_id = make_evidence_id(str(record["source"]), query_key, digest)
    relative = f"stage3_evidence_cache/sha256/{digest[:2]}/{digest}.json"
    metadata = {
        "evidence_id": evidence_id, "evidence_type": record["evidence_type"],
        "query_key": query_key, "source": record["source"],
        "lookup_status": record["lookup_status"], "queried_at": record["queried_at"],
        "payload_sha256": digest, "payload_path": relative,
        "error": str(record.get("error", "")), "schema_version": SCHEMA_VERSION,
    }
    return EvidenceItem(query_key, evidence_id, record, metadata, digest, payload)


def evaluate_advisory(
    author_date: str, evidence: Mapping[str, object]
) -> AdvisoryDecision:
    author = parse_time(author_date)
    if author is None:
        return AdvisoryDecision("hydration_incomplete", ("author_date_missing",))
    lookup = str(evidence.get("lookup_status", ""))
    if lookup == "unsupported":
        return AdvisoryDecision("not_queried", ("advisory_query_unsupported",))
    advisories = evidence.get("advisories", [])
    if not isinstance(advisories, list):
        raise ValueError("advisories must be a list")
    classified, incomplete = _classify_advisories(advisories, author)
    return _advisory_decision(lookup, classified, incomplete, advisories)


def _classify_advisories(
    advisories: Sequence[object], author: datetime
) -> tuple[list[tuple[str, Mapping[str, object]]], bool]:
    rows, incomplete = [], False
    for item in advisories:
        if not isinstance(item, Mapping):
            incomplete = True
            continue
        published = parse_time(str(item.get("published", "")))
        withdrawn = parse_time(str(item.get("withdrawn", "")))
        if published is None:
            incomplete = True
        elif published > author:
            rows.append(("future", item))
        elif withdrawn and withdrawn <= author:
            rows.append(("withdrawn", item))
        else:
            rows.append(("active", item))
    return rows, incomplete


def _advisory_decision(lookup, rows, incomplete, advisories) -> AdvisoryDecision:
    active = [item for status, item in rows if status == "active"]
    is_incomplete = incomplete or lookup in {"error", "hydration_incomplete"}
    is_incomplete = is_incomplete or lookup == "ok" and not _hydration_complete(advisories)
    if active:
        reasons = ("malicious_advisory_active",) if any(_is_malicious(item) for item in active) else ()
        if is_incomplete:
            reasons = tuple(sorted(set(reasons) | {"advisory_hydration_partial"}))
        return AdvisoryDecision("active_before_or_at_author_date", reasons)
    if is_incomplete or lookup != "ok":
        return AdvisoryDecision("hydration_incomplete", ("osv_hydration_failed",))
    if any(status == "withdrawn" for status, _item in rows):
        return AdvisoryDecision("withdrawn_before_author_date", ("advisory_withdrawn_before_commit",))
    if any(status == "future" for status, _item in rows):
        return AdvisoryDecision("published_after_author_date", ("advisory_published_after_commit",))
    return AdvisoryDecision("none_observed", ())


def _hydration_complete(advisories: Sequence[object]) -> bool:
    return all(
        isinstance(item, Mapping) and str(item.get("hydration_status", "ok")) == "ok"
        for item in advisories
    )


def _is_malicious(item: Mapping[str, object]) -> bool:
    if item.get("is_malicious") is True:
        return True
    details = item.get("database_specific", {})
    return isinstance(details, Mapping) and details.get("malicious") is True

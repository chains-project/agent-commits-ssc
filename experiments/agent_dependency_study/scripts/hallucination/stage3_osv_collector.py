"""Batch collection and hydration of OSV advisory evidence.

[IN]: Advisory queries, previous empty-result/ID indexes and OSV querybatch/query/vulns APIs.

[OUT]: Full advisory records per query and a separate gzip cache.

[POS]: Advisory I/O; OSV evidence remains separate from registry-time classification.

[SYNC]: Keep pagination, hydration and unsupported-query behavior aligned with tests.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from stage3_legacy_evidence import LegacyEvidenceIndex, canonical_package
from cache_storage import read_cache_text, write_gzip_text
from registry_label import OSV_ECOSYSTEMS, http_json


HttpJson = Callable[..., tuple[int, object, str]]


class OsvCollector:
    def __init__(
        self, cache_root: Path, legacy_index: LegacyEvidenceIndex, *,
        timeout: int = 25, retry_count: int = 2, retry_sleep: float = 0.5,
        http_call: HttpJson = http_json,
    ) -> None:
        self.cache_root = Path(cache_root)
        self.legacy_index = legacy_index
        self.timeout = timeout
        self.retry_count = retry_count
        self.retry_sleep = retry_sleep
        self.http_call = http_call

    def collect_batch(
        self, queries: Sequence[Mapping[str, str]]
    ) -> list[dict[str, object]]:
        output: dict[str, dict[str, object]] = {}
        pending = []
        for query in queries:
            key = _query_key(query)
            record = self._cached_or_reused(query)
            if record is not None:
                output[key] = record
            elif query["query_kind"] != "exact":
                output[key] = _unsupported(query)
            elif query["ecosystem"] not in OSV_ECOSYSTEMS:
                output[key] = _unsupported(query)
            else:
                pending.append(query)
        output.update(self._query_batch(pending))
        records = [output[_query_key(query)] for query in queries]
        for query, record in zip(queries, records):
            self._write_query_cache(query, record)
        return records

    def _cached_or_reused(self, query: Mapping[str, str]) -> dict[str, object] | None:
        cached = self._read_query_cache(query)
        if cached is not None:
            return cached
        if query["query_kind"] != "exact":
            return None
        legacy = self.legacy_index.advisory_record(
            query["ecosystem"], query["package_name"], query["query_value"]
        )
        if legacy is None:
            return None
        ids = legacy.get("advisory_ids", [])
        if not ids:
            return legacy
        advisories, complete = self._hydrate([str(item) for item in ids])
        return _record(query, advisories, "ok" if complete else "hydration_incomplete", "OSV_legacy_ids")

    def _query_batch(
        self, queries: Sequence[Mapping[str, str]]
    ) -> dict[str, dict[str, object]]:
        if not queries:
            return {}
        payload = {"queries": [_osv_query(query) for query in queries]}
        status, data, error = self._post("https://api.osv.dev/v1/querybatch", payload)
        if status != 200 or not isinstance(data, Mapping):
            return {_query_key(query): _error_record(query, error or str(status)) for query in queries}
        results = data.get("results", [])
        output = {}
        for index, query in enumerate(queries):
            result = results[index] if index < len(results) and isinstance(results[index], Mapping) else {}
            ids = [str(item.get("id")) for item in result.get("vulns", []) if isinstance(item, Mapping) and item.get("id")]
            ids.extend(self._query_pages(query, str(result.get("next_page_token", ""))))
            advisories, complete = self._hydrate(sorted(set(ids)))
            lookup = "ok" if complete else "hydration_incomplete"
            output[_query_key(query)] = _record(query, advisories, lookup, "OSV")
        return output

    def _query_pages(self, query: Mapping[str, str], token: str) -> list[str]:
        ids = []
        while token:
            payload = {**_osv_query(query), "page_token": token}
            status, data, _error = self._post("https://api.osv.dev/v1/query", payload)
            if status != 200 or not isinstance(data, Mapping):
                break
            ids.extend(
                str(item.get("id")) for item in data.get("vulns", [])
                if isinstance(item, Mapping) and item.get("id")
            )
            token = str(data.get("next_page_token", ""))
        return ids

    def _hydrate(self, ids: Sequence[str]) -> tuple[list[dict[str, object]], bool]:
        rows, complete = [], True
        for advisory_id in ids:
            record = self._read_vuln_cache(advisory_id)
            if record is None:
                record = self._fetch_vuln(advisory_id)
                self._write_vuln_cache(advisory_id, record)
            if record.get("hydration_status") != "ok":
                complete = False
            rows.append(record)
        return rows, complete

    def _fetch_vuln(self, advisory_id: str) -> dict[str, object]:
        encoded = urllib.parse.quote(advisory_id, safe="")
        status, data, error = self._get(f"https://api.osv.dev/v1/vulns/{encoded}")
        if status != 200 or not isinstance(data, Mapping):
            return {"id": advisory_id, "hydration_status": "error", "error": error or str(status)}
        record = dict(data)
        record["hydration_status"] = "ok"
        if advisory_id.startswith("MAL-"):
            record["is_malicious"] = True
        return record

    def _post(self, url: str, payload: Mapping[str, object]):
        call = lambda: self.http_call(url, self.timeout, method="POST", payload=payload)
        return _retry(call, self.retry_count, self.retry_sleep)

    def _get(self, url: str):
        call = lambda: self.http_call(url, self.timeout)
        return _retry(call, self.retry_count, self.retry_sleep)

    def _query_cache_path(self, query: Mapping[str, str]) -> Path:
        digest = hashlib.sha256(_query_key(query).encode("utf-8")).hexdigest()
        return self.cache_root / "osv" / "queries" / digest[:2] / f"{digest}.json"

    def _vuln_cache_path(self, advisory_id: str) -> Path:
        digest = hashlib.sha256(advisory_id.encode("utf-8")).hexdigest()
        return self.cache_root / "osv" / "vulns" / digest[:2] / f"{digest}.json"

    def _read_query_cache(self, query: Mapping[str, str]) -> dict[str, object] | None:
        return _read_json_cache(self._query_cache_path(query))

    def _write_query_cache(self, query: Mapping[str, str], record: Mapping[str, object]) -> None:
        _write_json_cache(self._query_cache_path(query), record)

    def _read_vuln_cache(self, advisory_id: str) -> dict[str, object] | None:
        return _read_json_cache(self._vuln_cache_path(advisory_id))

    def _write_vuln_cache(self, advisory_id: str, record: Mapping[str, object]) -> None:
        _write_json_cache(self._vuln_cache_path(advisory_id), record)


def _osv_query(query: Mapping[str, str]) -> dict[str, object]:
    package = {
        "name": query["package_name"],
        "ecosystem": OSV_ECOSYSTEMS[query["ecosystem"]],
    }
    return {"package": package, "version": query["query_value"]}


def _query_key(query: Mapping[str, str]) -> str:
    return "|".join((query["ecosystem"], canonical_package(query["ecosystem"], query["package_name"]), query["query_kind"], query["query_value"]))


def _record(query, advisories, lookup, source):
    return {
        "evidence_type": "advisory", "source": source,
        "ecosystem": query["ecosystem"], "package_name": query["package_name"],
        "query_kind": query["query_kind"], "query_value": query["query_value"],
        "lookup_status": lookup, "queried_at": _now(), "advisories": advisories,
    }


def _unsupported(query):
    return _record(query, [], "unsupported", "OSV")


def _error_record(query, error):
    return {**_record(query, [], "error", "OSV"), "error": error}


def _retry(call: Callable, count: int, delay: float):
    result = call()
    attempts = 0
    while result[0] in {0, 408, 429, 500, 502, 503, 504} and attempts < count:
        attempts += 1
        time.sleep(delay * attempts)
        result = call()
    return result


def _read_json_cache(path: Path) -> dict[str, object] | None:
    text, _actual = read_cache_text(path)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _write_json_cache(path: Path, record: Mapping[str, object]) -> None:
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    write_gzip_text(path, text)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

"""Collect or reuse package-level registry version snapshots for RQ2 v2.

[IN]: Unique Stage 2 package queries, legacy gzip caches, legacy exact-query
indexes, and network adapters.
[OUT]: Registry records satisfying the Stage 3 evidence contract and an
independent v2 gzip cache. Transient `lookup_status=error` entries are cache
misses and may be replaced by later successful queries.
[POS]: Stage 3 registry I/O layer; does not compute commit-time labels or
overwrite legacy rq3 caches.
[SYNC]: Keep collector tests, design documents, and OUTPUTS.md aligned when
registry sources or version-time semantics change.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from rq2_stage3_legacy_evidence import LegacyEvidenceIndex, canonical_package
from rq3_cache_storage import read_cache_text, write_gzip_text
from rq3_registry_label import cache_key, go_escape_module, http_json, http_text


JsonGetter = Callable[[str, int], tuple[int, object, str]]
TextGetter = Callable[[str, int], tuple[int, str, str]]
REGISTRY_CACHE_SCHEMA = 2
DEFAULT_GO_INFO_WORKERS = 8
DEFAULT_MAVEN_WORKERS = 1


class RegistryCollector:
    def __init__(
        self, cache_root: Path, legacy_cache_root: Path,
        legacy_index: LegacyEvidenceIndex, *, timeout: int = 25,
        retry_count: int = 2, retry_sleep: float = 0.5,
        go_info_workers: int = DEFAULT_GO_INFO_WORKERS,
        maven_workers: int = DEFAULT_MAVEN_WORKERS,
        json_get: JsonGetter = http_json, text_get: TextGetter = http_text,
    ) -> None:
        self.cache_root = Path(cache_root)
        self.legacy_cache_root = Path(legacy_cache_root)
        self.legacy_index = legacy_index
        self.timeout = timeout
        self.retry_count = retry_count
        self.retry_sleep = retry_sleep
        self.go_info_workers = max(1, int(go_info_workers))
        self.maven_workers = max(1, int(maven_workers))
        self.json_get = json_get
        self.text_get = text_get

    def collect(
        self, ecosystem: str, package: str,
        queries: Sequence[tuple[str, str]],
    ) -> dict[str, object]:
        cached = self._read_cached_record(ecosystem, package)
        if cached is not None:
            return cached
        record = self._reuse(ecosystem, package, queries)
        if record is None:
            record = self._fetch(ecosystem, package)
        self._write_v2_cache(ecosystem, package, record)
        self._write_go_module_cache(ecosystem, package, record)
        return record

    def collect_many(
        self,
        requests: Sequence[tuple[str, str, Sequence[tuple[str, str]]]],
    ) -> list[dict[str, object]]:
        results: list[dict[str, object] | None] = [None] * len(requests)
        maven = []
        for index, request in enumerate(requests):
            if request[0] == "Maven":
                maven.append((index, request))
            else:
                results[index] = self.collect(*request)
        self._collect_maven_many(maven, results)
        return [record for record in results if record is not None]

    def _collect_maven_many(self, indexed, results) -> None:
        if not indexed:
            return
        requests = [request for _index, request in indexed]
        if self.maven_workers == 1:
            records = [self.collect(*request) for request in requests]
        else:
            with ThreadPoolExecutor(max_workers=self.maven_workers) as executor:
                records = list(executor.map(self._collect_request, requests))
        for (index, _request), record in zip(indexed, records):
            results[index] = record

    def _collect_request(self, request) -> dict[str, object]:
        return self.collect(*request)

    def _read_cached_record(
        self, ecosystem: str, package: str,
    ) -> dict[str, object] | None:
        cached = self._read_v2_cache(ecosystem, package)
        if cached is not None or ecosystem != "Go":
            return cached
        for module in reversed(_go_module_candidates(package)):
            cached = self._read_v2_cache("Go", module)
            if cached is not None and cached.get("resolved_module") == module:
                return _record_for_package(cached, package)
        return None

    def _write_go_module_cache(
        self, ecosystem: str, package: str, record: Mapping[str, object],
    ) -> None:
        module = str(record.get("resolved_module", ""))
        if ecosystem != "Go" or not module or module == package:
            return
        if self._read_v2_cache("Go", module) is None:
            self._write_v2_cache("Go", module, _record_for_package(record, module))

    def _reuse(
        self, ecosystem: str, package: str, queries: Sequence[tuple[str, str]]
    ) -> dict[str, object] | None:
        if ecosystem in {"npm", "PyPI"}:
            record = self._legacy_full_snapshot(ecosystem, package)
            if record is not None:
                return record
        if queries and all(kind == "exact" for kind, _value in queries):
            versions = [value for _kind, value in queries]
            return self.legacy_index.exact_registry_record(ecosystem, package, versions)
        return None

    def _legacy_full_snapshot(
        self, ecosystem: str, package: str
    ) -> dict[str, object] | None:
        path = self._legacy_path(ecosystem, package)
        text, actual = read_cache_text(path)
        if text is None or actual is None:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        if int(payload.get("status", 0)) != 200:
            return None
        versions = _npm_versions(payload.get("data")) if ecosystem == "npm" else _pypi_versions(payload.get("data"))
        return _record(ecosystem, package, _source(ecosystem), "ok", versions, _mtime(actual))

    def _legacy_path(self, ecosystem: str, package: str) -> Path:
        if ecosystem == "npm":
            key = cache_key("npm", package)
            return self.legacy_cache_root / "npm" / f"{key}.json"
        normalized = canonical_package("PyPI", package)
        key = cache_key("pypi", normalized)
        return self.legacy_cache_root / "pypi" / f"{key}.json"

    def _fetch(self, ecosystem: str, package: str) -> dict[str, object]:
        handlers = {
            "npm": self._fetch_npm, "PyPI": self._fetch_pypi,
            "Cargo": self._fetch_cargo, "Go": self._fetch_go,
            "Maven": self._fetch_maven,
        }
        handler = handlers.get(ecosystem)
        if handler is None:
            return _record(ecosystem, package, "unsupported", "unsupported", [], _now())
        return handler(package)

    def _fetch_npm(self, package: str) -> dict[str, object]:
        url = f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='@')}"
        status, data, error = self._json(url)
        versions = _npm_versions(data) if status == 200 else []
        return _http_record("npm", package, "registry.npmjs.org", status, versions, error)

    def _fetch_pypi(self, package: str) -> dict[str, object]:
        normalized = canonical_package("PyPI", package)
        url = f"https://pypi.org/pypi/{urllib.parse.quote(normalized)}/json"
        status, data, error = self._json(url)
        versions = _pypi_versions(data) if status == 200 else []
        return _http_record("PyPI", package, "pypi.org", status, versions, error)

    def _fetch_cargo(self, package: str) -> dict[str, object]:
        url = f"https://crates.io/api/v1/crates/{urllib.parse.quote(package)}"
        status, data, error = self._json(url)
        versions = _cargo_versions(data) if status == 200 else []
        return _http_record("Cargo", package, "crates.io", status, versions, error)

    def _fetch_go(self, package: str) -> dict[str, object]:
        last_status, last_error = 404, ""
        for module in _go_module_candidates(package):
            escaped = _go_proxy_path(module)
            url = f"https://proxy.golang.org/{escaped}/@v/list"
            status, body, error = self._text(url)
            if status == 200:
                versions = self._go_versions(escaped, body.splitlines())
                record = _http_record("Go", package, "proxy.golang.org", 200, versions, "")
                record["resolved_module"] = module
                return record
            last_status, last_error = status, error
        return _http_record("Go", package, "proxy.golang.org", last_status, [], last_error)

    def _go_versions(self, escaped: str, names: Sequence[str]) -> list[dict[str, str]]:
        versions = sorted({item.strip() for item in names if item.strip()})
        if len(versions) < 2 or self.go_info_workers == 1:
            return [self._go_version(escaped, version) for version in versions]
        with ThreadPoolExecutor(max_workers=self.go_info_workers) as executor:
            return list(executor.map(
                lambda version: self._go_version(escaped, version), versions
            ))

    def _go_version(self, escaped: str, version: str) -> dict[str, str]:
        url = f"https://proxy.golang.org/{escaped}/@v/{urllib.parse.quote(version)}.info"
        status, data, _error = self._json(url)
        published = str(data.get("Time", "")) if status == 200 and isinstance(data, Mapping) else ""
        return {"version": version, "published_at": published}

    def _fetch_maven(self, package: str) -> dict[str, object]:
        if ":" not in package:
            return _record("Maven", package, "search.maven.org", "error", [], _now(), "expected group:artifact")
        group, artifact = package.split(":", 1)
        docs, status, error = self._maven_pages(group, artifact)
        versions = [_maven_version(item) for item in docs if item.get("v")]
        return _http_record("Maven", package, "search.maven.org", status, versions, error)

    def _maven_pages(self, group: str, artifact: str) -> tuple[list[Mapping[str, object]], int, str]:
        rows, start, total = [], 0, 1
        while start < total:
            query = f'g:"{group}" AND a:"{artifact}"'
            params = {"q": query, "core": "gav", "rows": "200", "start": str(start), "wt": "json"}
            status, data, error = self._json("https://search.maven.org/solrsearch/select?" + urllib.parse.urlencode(params))
            if status != 200 or not isinstance(data, Mapping):
                return rows, status, error
            response = data.get("response", {})
            docs = response.get("docs", []) if isinstance(response, Mapping) else []
            rows.extend(item for item in docs if isinstance(item, Mapping))
            total = int(response.get("numFound", len(rows))) if isinstance(response, Mapping) else len(rows)
            start += len(docs)
            if not docs:
                break
        return rows, 200, ""

    def _json(self, url: str) -> tuple[int, object, str]:
        return _retry(lambda: self.json_get(url, self.timeout), self.retry_count, self.retry_sleep)

    def _text(self, url: str) -> tuple[int, str, str]:
        return _retry(lambda: self.text_get(url, self.timeout), self.retry_count, self.retry_sleep)

    def _cache_path(self, ecosystem: str, package: str) -> Path:
        identity = f"{ecosystem}|{canonical_package(ecosystem, package)}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self.cache_root / "registry" / ecosystem.lower() / digest[:2] / f"{digest}.json"

    def _read_v2_cache(self, ecosystem: str, package: str) -> dict[str, object] | None:
        text, _actual = read_cache_text(self._cache_path(ecosystem, package))
        if text is None:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict) or value.get("cache_schema") != REGISTRY_CACHE_SCHEMA:
            return None
        record = value.get("record")
        if not isinstance(record, dict) or record.get("lookup_status") == "error":
            return None
        return record

    def _write_v2_cache(self, ecosystem: str, package: str, record: Mapping[str, object]) -> None:
        payload = {"cache_schema": REGISTRY_CACHE_SCHEMA, "record": record}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        write_gzip_text(self._cache_path(ecosystem, package), text)


def _retry(
    call: Callable, count: int, delay: float, *,
    sleep_func: Callable[[float], None] = time.sleep,
    random_func: Callable[[], float] = random.random,
):
    result = call()
    attempts = 0
    while result[0] in {0, 408, 429, 500, 502, 503, 504} and attempts < count:
        attempts += 1
        sleep_func(_retry_delay(result, attempts, delay, random_func))
        result = call()
    return result


def _retry_delay(result, attempt: int, base: float, random_func) -> float:
    retry_after = _retry_after(result[1])
    if retry_after is not None:
        return retry_after
    return base * (2 ** (attempt - 1)) + base * random_func()


def _retry_after(payload: object) -> float | None:
    if not isinstance(payload, Mapping):
        return None
    try:
        value = float(payload.get("_retry_after", ""))
    except (TypeError, ValueError):
        return None
    return max(0.0, value)


def _record_for_package(
    record: Mapping[str, object], package: str,
) -> dict[str, object]:
    return {**record, "package_name": package}


def _go_proxy_path(module: str) -> str:
    return urllib.parse.quote(go_escape_module(module), safe="/!")


def _npm_versions(data: object) -> list[dict[str, str]]:
    if not isinstance(data, Mapping):
        return []
    if data.get("_cache_schema") == "npm-minimal-v1":
        names, times = data.get("version_names", []), data.get("version_times", {})
    else:
        names, times = (data.get("versions", {}) or {}).keys(), data.get("time", {}) or {}
    return [{"version": str(name), "published_at": str(times.get(name, ""))} for name in names]


def _pypi_versions(data: object) -> list[dict[str, str]]:
    releases = data.get("releases", {}) if isinstance(data, Mapping) else {}
    rows = []
    for version, files in releases.items():
        times = [str(item.get("upload_time_iso_8601", "")) for item in files if item.get("upload_time_iso_8601")]
        rows.append({"version": str(version), "published_at": min(times) if times else ""})
    return rows


def _cargo_versions(data: object) -> list[dict[str, str]]:
    values = data.get("versions", []) if isinstance(data, Mapping) else []
    return [
        {"version": str(item.get("num", "")), "published_at": str(item.get("created_at", ""))}
        for item in values if isinstance(item, Mapping) and item.get("num")
    ]


def _go_module_candidates(package: str) -> list[str]:
    parts = package.strip("/").split("/")
    values = [package.strip("/")]
    if len(parts) >= 3 and parts[0] in {"github.com", "gitlab.com", "bitbucket.org"}:
        root = parts[:3]
        if len(parts) > 3 and parts[3].startswith("v") and parts[3][1:].isdigit():
            root.append(parts[3])
        values.append("/".join(root))
    return list(dict.fromkeys(values))


def _maven_version(item: Mapping[str, object]) -> dict[str, str]:
    stamp = item.get("timestamp")
    published = ""
    if isinstance(stamp, (int, float)):
        published = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {"version": str(item.get("v", "")), "published_at": published}


def _http_record(ecosystem, package, source, status, versions, error):
    lookup = "ok" if status == 200 else ("not_found" if status == 404 else "error")
    return _record(ecosystem, package, source, lookup, versions, _now(), error)


def _record(ecosystem, package, source, status, versions, queried_at, error=""):
    return {
        "evidence_type": "registry", "source": source,
        "ecosystem": ecosystem, "package_name": package,
        "lookup_status": status, "queried_at": queried_at,
        "versions": versions, "error": error,
    }


def _source(ecosystem: str) -> str:
    return {"npm": "registry.npmjs.org_legacy_cache", "PyPI": "pypi.org_legacy_cache"}[ecosystem]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")

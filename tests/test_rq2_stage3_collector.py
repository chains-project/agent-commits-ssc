"""Test RQ2 v2 Stage 3 legacy evidence reuse, adapters, and checkpoints."""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "hallucination"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import rq2_stage3_collect as collect
import rq2_stage3_registry_collector as registry
from rq2_stage3_legacy_evidence import LegacyEvidenceIndex
from rq2_stage3_osv_collector import OsvCollector
from rq2_stage3_registry_collector import RegistryCollector, _go_module_candidates
from rq3_cache_storage import write_gzip_text


REGISTRY_FIELDS = (
    "ecosystem", "package_name", "version", "package_exists_current_registry",
    "version_exists_current_registry", "publish_time", "registry_source",
)
ADVISORY_FIELDS = (
    "ecosystem", "package_name", "version", "osv_status", "advisory_ids",
)


class Stage3CollectorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.legacy_output = self.root / "legacy_output"
        self.legacy_output.mkdir()
        _write_csv(
            self.legacy_output / "registry_lookup_results.csv", REGISTRY_FIELDS,
            [
                {"ecosystem": "Cargo", "package_name": "serde", "version": "1.0.0",
                 "package_exists_current_registry": "True", "version_exists_current_registry": "True",
                 "publish_time": "2020-01-01T00:00:00Z", "registry_source": "crates.io"},
            ],
        )
        _write_csv(
            self.legacy_output / "advisory_lookup_results.csv", ADVISORY_FIELDS,
            [
                {"ecosystem": "Cargo", "package_name": "serde", "version": "1.0.0",
                 "osv_status": "200", "advisory_ids": ""},
            ],
        )
        self.legacy = LegacyEvidenceIndex.from_directories([self.legacy_output])

    def tearDown(self):
        self.temporary.cleanup()

    def test_exact_registry_and_zero_advisory_are_reused(self):
        registry = self.legacy.exact_registry_record("Cargo", "serde", ["1.0.0"])
        advisory = self.legacy.advisory_record("Cargo", "serde", "1.0.0")
        self.assertEqual(registry["versions"][0]["version"], "1.0.0")
        self.assertEqual(registry["reuse_scope"], "exact_queries_only")
        self.assertEqual(advisory["lookup_status"], "ok")
        self.assertEqual(advisory["advisories"], [])

    def test_npm_full_legacy_cache_avoids_network(self):
        legacy_cache = self.root / "legacy_cache"
        payload = {
            "status": 200, "error": "",
            "data": {"_cache_schema": "npm-minimal-v1", "version_names": ["1.0.0"],
                     "version_times": {"1.0.0": "2020-01-01T00:00:00Z"}},
        }
        write_gzip_text(
            legacy_cache / "npm" / "npm__demo.json",
            json.dumps(payload),
        )
        collector = RegistryCollector(
            self.root / "v2", legacy_cache, self.legacy,
            json_get=_fail_network, text_get=_fail_network,
        )
        record = collector.collect("npm", "demo", [("range", "^1.0.0")])
        self.assertEqual(record["lookup_status"], "ok")
        self.assertEqual(record["versions"][0]["published_at"], "2020-01-01T00:00:00Z")

    def test_cached_transient_maven_error_is_refetched_and_replaced(self):
        cache = self.root / "maven-v2"
        timed_out = RegistryCollector(
            cache, self.root / "legacy", self.legacy,
            retry_count=0,
            json_get=lambda _url, _timeout: (0, {}, "The read operation timed out"),
        ).collect("Maven", "org.example:demo", [("name_only", "")])
        self.assertEqual(timed_out["lookup_status"], "error")

        calls = []

        def success(_url, _timeout):
            calls.append("fetch")
            return 200, _maven_response("1.2.3"), ""

        recovered = RegistryCollector(
            cache, self.root / "legacy", self.legacy, json_get=success,
        ).collect("Maven", "org.example:demo", [("name_only", "")])
        self.assertEqual(recovered["lookup_status"], "ok")
        self.assertEqual(recovered["versions"][0]["version"], "1.2.3")
        self.assertEqual(calls, ["fetch"])

        reused = RegistryCollector(
            cache, self.root / "legacy", self.legacy, json_get=_fail_network,
        ).collect("Maven", "org.example:demo", [("name_only", "")])
        self.assertEqual(reused["lookup_status"], "ok")

    def test_cached_not_found_remains_reusable(self):
        cache = self.root / "cargo-v2"
        first = RegistryCollector(
            cache, self.root / "legacy", self.legacy,
            json_get=lambda _url, _timeout: (404, {}, "not found"),
        ).collect("Cargo", "missing-demo", [("name_only", "")])
        self.assertEqual(first["lookup_status"], "not_found")

        second = RegistryCollector(
            cache, self.root / "legacy", self.legacy, json_get=_fail_network,
        ).collect("Cargo", "missing-demo", [("name_only", "")])
        self.assertEqual(second["lookup_status"], "not_found")

    def test_osv_querybatch_hydrates_full_record(self):
        calls = []

        def fake_http(url, _timeout, method="GET", payload=None):
            calls.append((url, method, payload))
            if url.endswith("querybatch"):
                return 200, {"results": [{"vulns": [{"id": "GHSA-demo"}]}]}, ""
            return 200, {"id": "GHSA-demo", "published": "2020-01-01T00:00:00Z"}, ""

        collector = OsvCollector(self.root / "v2", self.legacy, http_call=fake_http)
        query = _query("npm", "demo", "exact", "1.0.0")
        record = collector.collect_batch([query])[0]
        self.assertEqual(record["lookup_status"], "ok")
        self.assertEqual(record["advisories"][0]["hydration_status"], "ok")
        self.assertEqual(len(calls), 2)

    def test_non_exact_osv_query_is_explicitly_unsupported(self):
        collector = OsvCollector(self.root / "v2", self.legacy, http_call=_fail_network)
        record = collector.collect_batch([_query("npm", "demo", "range", "^1")])[0]
        self.assertEqual(record["lookup_status"], "unsupported")
        self.assertEqual(record["advisories"], [])

    def test_go_subpackage_falls_back_to_module_root(self):
        self.assertEqual(
            _go_module_candidates("github.com/stretchr/testify/assert"),
            ["github.com/stretchr/testify/assert", "github.com/stretchr/testify"],
        )
        self.assertEqual(
            _go_module_candidates("github.com/urfave/cli/v3"),
            ["github.com/urfave/cli/v3"],
        )

    def test_go_unicode_module_path_is_percent_encoded(self):
        calls = []

        def fake_text(url, _timeout):
            calls.append(url)
            return 404, "", "not found"

        collector = RegistryCollector(
            self.root / "go-unicode", self.root / "legacy", self.legacy,
            json_get=_fail_network, text_get=fake_text,
        )
        record = collector.collect(
            "Go", "p9e.in/Samavāya/packages/events", [("name_only", "")]
        )

        self.assertEqual(record["lookup_status"], "not_found")
        self.assertEqual(calls, [
            "https://proxy.golang.org/p9e.in/!samav%C4%81ya/packages/events/@v/list"
        ])
        self.assertTrue(calls[0].isascii())

    def test_go_resolved_module_snapshot_is_reused_for_sibling_subpackage(self):
        text_calls, json_calls = [], []

        def fake_text(url, _timeout):
            text_calls.append(url)
            if "testify/assert" in url:
                return 404, "", "not found"
            return 200, "v1.9.0\n", ""

        def fake_json(url, _timeout):
            json_calls.append(url)
            return 200, {"Version": "v1.9.0", "Time": "2020-01-01T00:00:00Z"}, ""

        cache = self.root / "go-v2"
        first = RegistryCollector(
            cache, self.root / "legacy", self.legacy,
            json_get=fake_json, text_get=fake_text,
        ).collect(
            "Go", "github.com/stretchr/testify/assert", [("name_only", "")]
        )
        second = RegistryCollector(
            cache, self.root / "legacy", self.legacy,
            json_get=_fail_network, text_get=_fail_network,
        ).collect(
            "Go", "github.com/stretchr/testify/require", [("name_only", "")]
        )
        self.assertEqual(first["resolved_module"], "github.com/stretchr/testify")
        self.assertEqual(second["resolved_module"], "github.com/stretchr/testify")
        self.assertEqual(second["package_name"], "github.com/stretchr/testify/require")
        self.assertEqual(len(text_calls), 2)
        self.assertEqual(len(json_calls), 1)

    def test_go_module_error_cache_is_ignored_and_replaced(self):
        cache = self.root / "go-error-v2"
        collector = RegistryCollector(
            cache, self.root / "legacy", self.legacy,
            json_get=lambda _url, _timeout: (
                200, {"Time": "2020-01-01T00:00:00Z"}, ""
            ),
            text_get=lambda url, _timeout: (
                (404, "", "not found") if "testify/require" in url
                else (200, "v1.9.0\n", "")
            ),
        )
        collector._write_v2_cache(
            "Go", "github.com/stretchr/testify",
            _registry_error(
                "Go", "github.com/stretchr/testify", "proxy.golang.org",
                resolved_module="github.com/stretchr/testify",
            ),
        )
        recovered = collector.collect(
            "Go", "github.com/stretchr/testify/require", [("name_only", "")]
        )
        self.assertEqual(recovered["lookup_status"], "ok")
        self.assertEqual(recovered["resolved_module"], "github.com/stretchr/testify")

        sibling = RegistryCollector(
            cache, self.root / "legacy", self.legacy,
            json_get=_fail_network, text_get=_fail_network,
        ).collect(
            "Go", "github.com/stretchr/testify/assert", [("name_only", "")]
        )
        self.assertEqual(sibling["lookup_status"], "ok")
        self.assertEqual(sibling["resolved_module"], "github.com/stretchr/testify")

    def test_go_version_info_fetch_is_bounded_parallel_and_deterministic(self):
        lock = threading.Lock()
        active = 0
        maximum = 0

        def fake_json(url, _timeout):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.03)
                version = url.rsplit("/", 1)[-1].removesuffix(".info")
                return 200, {"Time": f"2020-01-0{version[-1]}T00:00:00Z"}, ""
            finally:
                with lock:
                    active -= 1

        collector = RegistryCollector(
            self.root / "go-parallel", self.root / "legacy", self.legacy,
            go_info_workers=3, json_get=fake_json,
            text_get=lambda _url, _timeout: (200, "v1.0.3\nv1.0.1\nv1.0.2\n", ""),
        )
        record = collector.collect("Go", "example.com/module", [("name_only", "")])

        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, 3)
        self.assertEqual(
            [row["version"] for row in record["versions"]],
            ["v1.0.1", "v1.0.2", "v1.0.3"],
        )

    def test_maven_package_fetch_is_bounded_parallel_and_deterministic(self):
        lock = threading.Lock()
        active = 0
        maximum = 0

        def fake_json(url, _timeout):
            nonlocal active, maximum
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(url).query
            )["q"][0]
            artifact = query.split('a:"', 1)[1].split('"', 1)[0]
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.03)
                return 200, _maven_response(artifact), ""
            finally:
                with lock:
                    active -= 1

        collector = RegistryCollector(
            self.root / "maven-parallel", self.root / "legacy", self.legacy,
            maven_workers=2, json_get=fake_json,
        )
        requests = [
            ("Maven", f"org.example:{name}", [("name_only", "")])
            for name in ("first", "second", "third", "fourth")
        ]
        records = collector.collect_many(requests)

        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, 2)
        self.assertEqual(
            [row["package_name"] for row in records],
            [request[1] for request in requests],
        )
        self.assertEqual(
            [row["versions"][0]["version"] for row in records],
            ["first", "second", "third", "fourth"],
        )

    def test_retry_honors_retry_after_then_uses_exponential_backoff(self):
        responses = iter([
            (503, {"_retry_after": "3"}, "busy"),
            (503, {}, "busy"),
            (200, {"ok": True}, ""),
        ])
        sleeps = []
        result = registry._retry(
            lambda: next(responses), 2, 1.0,
            sleep_func=sleeps.append, random_func=lambda: 0.0,
        )
        self.assertEqual(result[0], 200)
        self.assertEqual(sleeps, [3.0, 2.0])

    def test_registry_transport_error_stays_retryable_and_out_of_evidence(self):
        connection = sqlite3.connect(self.root / "retry.sqlite")
        output = self.root / "retry.jsonl"
        try:
            collect._initialize(connection)
            _insert_registry_query(connection, "bad")
            _insert_registry_query(connection, "good")
            connection.commit()
            complete = collect._collect_registry(
                connection, output, _FakeRegistry({"bad"}), 10, 0,
                error_rate_min_samples=500, error_rate_threshold=0.02,
            )
            states = dict(connection.execute(
                "SELECT package_name,status FROM query ORDER BY package_name"
            ))
            rows = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]
            self.assertFalse(complete)
            self.assertEqual(states, {"bad": "retry", "good": "done"})
            self.assertEqual([row["package_name"] for row in rows], ["good"])
            attempt = connection.execute(
                "SELECT attempt_count,last_error FROM query_attempt "
                "WHERE query_key='registry|Maven|bad'"
            ).fetchone()
            self.assertEqual(attempt, (1, "The read operation timed out"))
        finally:
            connection.close()

    def test_registry_error_rate_triggers_controlled_pause(self):
        connection = sqlite3.connect(self.root / "pause.sqlite")
        output = self.root / "pause.jsonl"
        try:
            collect._initialize(connection)
            for package in ("a", "b", "c", "d"):
                _insert_registry_query(connection, package)
            connection.commit()
            with self.assertRaises(collect.CollectorQualityPause) as raised:
                collect._collect_registry(
                    connection, output,
                    _FakeRegistry({"a", "b", "c", "d"}), 4, 0,
                    error_rate_min_samples=4, error_rate_threshold=0.20,
                )
            self.assertEqual(raised.exception.details["observed"], 4)
            self.assertEqual(raised.exception.details["errors"], 4)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM query WHERE status='retry'"
                ).fetchone()[0],
                4,
            )
            self.assertFalse(output.exists())
        finally:
            connection.close()

    def test_evidence_journal_truncates_uncommitted_tail(self):
        database = self.root / "state.sqlite"
        output = self.root / "evidence.jsonl"
        connection = sqlite3.connect(database)
        try:
            collect._initialize(connection)
            row = _query("npm", "demo", "exact", "1.0.0")
            row["query_key"] = "registry|npm|demo"
            connection.execute(
                "INSERT INTO query VALUES(?,?,?,?,?,?,?)",
                (row["query_key"], "registry", "npm", "demo", "", "", "pending"),
            )
            connection.commit()
            record = {
                "evidence_type": "registry", "source": "fixture", "ecosystem": "npm",
                "package_name": "demo", "lookup_status": "ok",
                "queried_at": "2026-08-04T00:00:00Z", "versions": [],
            }
            collect._commit_records(connection, output, [row], [record])
            committed = output.stat().st_size
            with output.open("ab") as handle:
                handle.write(b"uncommitted")
            collect._resume_output(connection, output)
            self.assertEqual(output.stat().st_size, committed)
        finally:
            connection.close()


def _write_csv(path: Path, fields, rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _query(ecosystem, package, kind, value):
    return {
        "ecosystem": ecosystem, "package_name": package,
        "query_kind": kind, "query_value": value,
    }


def _maven_response(version):
    return {
        "response": {
            "numFound": 1,
            "docs": [{"v": version, "timestamp": 1_577_836_800_000}],
        }
    }


def _registry_error(ecosystem, package, source, **extra):
    return {
        "evidence_type": "registry", "source": source,
        "ecosystem": ecosystem, "package_name": package,
        "lookup_status": "error", "queried_at": "2026-08-11T00:00:00Z",
        "versions": [], "error": "The read operation timed out", **extra,
    }


def _insert_registry_query(connection, package):
    key = f"registry|Maven|{package}"
    connection.execute(
        "INSERT INTO query VALUES(?,?,?,?,?,?,?)",
        (key, "registry", "Maven", package, "", "", "pending"),
    )
    connection.execute(
        "INSERT INTO registry_value VALUES(?,?,?)",
        (key, "name_only", ""),
    )


class _FakeRegistry:
    def __init__(self, failing):
        self.failing = set(failing)

    def collect_many(self, requests):
        records = []
        for ecosystem, package, _values in requests:
            if package in self.failing:
                records.append(_registry_error(
                    ecosystem, package, "search.maven.org"
                ))
            else:
                records.append({
                    "evidence_type": "registry",
                    "source": "search.maven.org",
                    "ecosystem": ecosystem,
                    "package_name": package,
                    "lookup_status": "ok",
                    "queried_at": "2026-08-14T00:00:00Z",
                    "versions": [],
                    "error": "",
                })
        return records


def _fail_network(*_args, **_kwargs):
    raise AssertionError("network should not be called")


if __name__ == "__main__":
    unittest.main()

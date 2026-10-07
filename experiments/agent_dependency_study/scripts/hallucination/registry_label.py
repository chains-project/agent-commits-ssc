"""
[IN]: import_dependency_matches.csv from manifest_enrich.py.
[OUT]: bom_dependency_management_results.csv, registry_lookup_results.csv,
advisory_lookup_results.csv, final_labels.csv, hallucination_pipeline_report.md, and
the shared gzip THESIS_DATA_ROOT/cache/rq2_hallucinated/registry cache.
[POS]: Third-stage evidence labeling for the Hallucinated Dependencies pipeline.
[SYNC]: If labels, registry fields, or advisory fields change, update weekly-report/w5-pipeline-overview-hallucinated-dependencies.md and scripts/OUTPUTS.md.

This stage is intentionally conservative. Exact versions published no later
than author_date provide strong existence evidence. Versions published later
than author_date remain history-validation candidates until committer, parent,
root-history, and mapping checks are complete. Ranges, mapping-unknown cases,
private/local specs, and missing historical evidence remain non-conclusive.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import http.client
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def raise_csv_field_limit() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


raise_csv_field_limit()
from typing import Any, Callable, Iterable

from cache_storage import read_cache_text, write_gzip_text


DEFAULT_INPUT = Path("data_products/rq2_hallucinated_manifest_smoke_tree")
DATA_ROOT = Path(os.environ.get("THESIS_DATA_ROOT", r"D:\MasterThesis\thesis-work-data"))
OSV_ECOSYSTEMS = {"npm": "npm", "PyPI": "PyPI", "Cargo": "crates.io", "Go": "Go", "Maven": "Maven", "NuGet": "NuGet", "Packagist": "Packagist"}
SUPPORTED_REGISTRIES = {"npm", "PyPI", "Cargo", "Go", "Maven", "NuGet", "Packagist"}
DEPSDEV_SYSTEMS = {"npm": "NPM", "PyPI": "PYPI", "Cargo": "CARGO", "Go": "GO", "Maven": "MAVEN", "NuGet": "NUGET"}
PRIVATE_SPEC_PREFIXES = ("file:", "workspace:", "path:", "git:", "http:", "https:", "../", "./", "/")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=DATA_ROOT / "cache" / "rq2_hallucinated" / "registry")
    parser.add_argument("--max-rows", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--sleep-seconds", type=float, default=0.15)
    parser.add_argument("--retry-count", type=int, default=1)
    parser.add_argument("--retry-sleep-seconds", type=float, default=0.5)
    parser.add_argument("--osv-batch-size", type=int, default=100)
    parser.add_argument("--depsdev-mode", choices=["off", "uncertain", "all"], default="uncertain")
    parser.add_argument("--checkpoint-every-registry-rows", type=int, default=100,
                        help="Write Stage3 registry CSV checkpoints every N lookup rows; 0 disables mid-run checkpoints.")
    parser.add_argument("--checkpoint-every-osv-batches", type=int, default=5,
                        help="Write Stage3 advisory CSV checkpoints every N OSV batches; 0 disables mid-run checkpoints.")
    return parser.parse_args()


def clean_lines(path: Path) -> Iterable[str]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for line in handle:
            yield line.replace("\x00", "")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return list(csv.DictReader(clean_lines(path)))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def advisory_rows_from_map(advisory_map: dict[tuple[str, str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"ecosystem": eco, "package_name": package, "version": version, **values}
        for (eco, package, version), values in advisory_map.items()
    ]


def write_stage3_checkpoint(output_dir: Path, phase: str, registry_rows: list[dict[str, Any]], advisory_rows: list[dict[str, Any]], final_rows: list[dict[str, Any]] | None = None, complete: bool = False) -> None:
    checkpoint = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "registry_lookup_rows": len(registry_rows),
        "advisory_lookup_rows": len(advisory_rows),
        "final_label_rows": len(final_rows or []),
        "complete": complete,
    }
    (output_dir / "stage3_checkpoint.json").write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")


def write_stage3_registry_outputs(output_dir: Path, bom_rows: list[dict[str, Any]], registry_rows: list[dict[str, Any]]) -> None:
    write_csv(output_dir / "bom_dependency_management_results.csv", bom_rows, BOM_RESULT_FIELDS)
    write_csv(output_dir / "registry_lookup_results.csv", registry_rows)


def http_json(url: str, timeout: int, method: str = "GET", payload: Any | None = None) -> tuple[int, Any, str]:
    data = None
    headers = {"User-Agent": "rq3-hallucinated-dependency-pipeline/0.1"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body) if body else {}, ""
    except urllib.error.HTTPError as error:
        return error.code, http_error_json(error), str(error)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, http.client.InvalidURL) as error:
        return 0, {}, str(error)


def http_text(url: str, timeout: int) -> tuple[int, str, str]:
    headers = {"User-Agent": "rq3-hallucinated-dependency-pipeline/0.1"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace"), ""
    except urllib.error.HTTPError as error:
        try:
            body = error.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return error.code, body, str(error)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.InvalidURL) as error:
        return 0, "", str(error)


def read_error_json(error: urllib.error.HTTPError) -> Any:
    try:
        body = error.read().decode("utf-8", errors="replace")
        return json.loads(body) if body else {}
    except Exception:
        return {}


def http_error_json(error: urllib.error.HTTPError) -> Any:
    payload = read_error_json(error)
    retry_after = error.headers.get("Retry-After", "") if error.headers else ""
    if not retry_after:
        return payload
    if not isinstance(payload, dict):
        payload = {"error_payload": payload}
    return {**payload, "_retry_after": retry_after}


def cache_key(*parts: str) -> str:
    value = "__".join(parts)
    return re.sub(r"[^A-Za-z0-9_.@-]+", "_", value).strip("_") or "_"


def cached_http_json(
    cache_dir: Path,
    key_parts: tuple[str, ...],
    url: str,
    timeout: int,
    retry_count: int = 0,
    retry_sleep: float = 0.5,
    data_transform: Callable[[Any], Any] | None = None,
) -> tuple[int, Any, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{cache_key(*key_parts)}.json"
    cached_text, _actual_path = read_cache_text(path)
    if cached_text is not None:
        try:
            cached = json.loads(cached_text)
            status = int(cached.get("status", 0))
            error = cached.get("error", "")
            if not should_retry_status(status, error):
                data = cached.get("data", {})
                return status, data_transform(data) if data_transform else data, error
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    status, data, error = http_json(url, timeout)
    attempts = 0
    while should_retry_status(status, error) and attempts < retry_count:
        attempts += 1
        time.sleep(retry_sleep)
        status, data, error = http_json(url, timeout)
    stored_data = data_transform(data) if data_transform else data
    payload = json.dumps({"status": status, "data": stored_data, "error": error}, ensure_ascii=False)
    write_gzip_text(path, payload)
    return status, stored_data, error


def cached_http_text(
    cache_dir: Path,
    key_parts: tuple[str, ...],
    url: str,
    timeout: int,
    retry_count: int = 0,
    retry_sleep: float = 0.5,
) -> tuple[int, str, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{cache_key(*key_parts)}.txt"
    cached_text, _actual_path = read_cache_text(path)
    if cached_text is not None:
        try:
            cached = json.loads(cached_text)
            status = int(cached.get("status", 0))
            error = cached.get("error", "")
            if not should_retry_status(status, error):
                return status, str(cached.get("text", "")), error
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    status, text, error = http_text(url, timeout)
    attempts = 0
    while should_retry_status(status, error) and attempts < retry_count:
        attempts += 1
        time.sleep(retry_sleep)
        status, text, error = http_text(url, timeout)
    payload = json.dumps({"status": status, "text": text, "error": error}, ensure_ascii=False)
    write_gzip_text(path, payload)
    return status, text, error

def should_retry_status(status: Any, error: str = "") -> bool:
    text = str(status)
    if text in {"", "0", "408", "409", "425", "429"}:
        return True
    if text.startswith("5"):
        return True
    lowered = (error or "").lower()
    return any(marker in lowered for marker in ["timed out", "timeout", "temporarily", "connection reset", "remote end closed"])



def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    text = normalize_fractional_seconds(text)
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)



def normalize_fractional_seconds(text: str) -> str:
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d{1,6})([+-]\d{2}:\d{2})$", text)
    if not match:
        return text
    prefix, fraction, suffix = match.groups()
    return f"{prefix}.{fraction.ljust(6, '0')}{suffix}"

def normalize_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def exact_version(spec: str, version_kind: str = "") -> str:
    value = (spec or "").strip().strip('"').strip("'")
    if not value:
        return ""
    for prefix in ("===", "==", "="):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    if version_kind == "resolved":
        return value
    if re.fullmatch(r"v?\d+(?:\.\d+)*(?:[-+][A-Za-z0-9_.-]+)?", value):
        return value
    return ""


def is_private_or_local_spec(spec: str, version_kind: str) -> bool:
    value = (spec or "").strip().lower()
    if version_kind == "non_registry":
        return True
    if any(marker in value for marker in ("workspace", "path =", "git =")):
        return True
    return value.startswith(PRIVATE_SPEC_PREFIXES) or " git+" in value or value.startswith("git+")


def is_first_party_dependency(row: dict[str, str]) -> bool:
    return row.get("resolution_source") == "first_party_module" or row.get("declared_dependency_match") == "first_party_module_import"


def is_gradle_catalog_possible_dependency(row: dict[str, str]) -> bool:
    return str(row.get("dependency_group", "")).startswith("gradle.version_catalog:")


def is_bom_managed_dependency(row: dict[str, str]) -> bool:
    return row.get("version_kind") == "bom_managed" or "bom_managed_by:" in str(row.get("dependency_group", ""))


def is_maven_snapshot_version(row: dict[str, str]) -> bool:
    version = row.get("query_version") or query_version_for(row)
    return row.get("ecosystem") == "Maven" and version.upper().endswith("-SNAPSHOT")


def is_npm_custom_registry_dependency(row: dict[str, str]) -> bool:
    return row.get("ecosystem") == "npm" and "npm.custom_registry:" in str(row.get("dependency_group", ""))


def is_npm_alias_dependency(row: dict[str, str]) -> bool:
    return (
        row.get("ecosystem") == "npm"
        and "npm.alias" in str(row.get("dependency_group", ""))
        and bool(row.get("namespace_hint"))
    )


def dependency_not_introduced_in_commit(row: dict[str, str]) -> bool:
    if not str(row.get("declared_dependency_match", "")).startswith("observed_in"):
        return False
    changed = str(row.get("dependency_file_changed_in_diff", "")).strip().lower()
    return changed == "false"


def is_mapping_uncertain(row: dict[str, str]) -> bool:
    match_label = row.get("declared_dependency_match", "")
    if match_label.startswith("observed_in") or match_label == "first_party_module_import":
        return False
    return (not row.get("declared_package") and not row.get("package_candidate")) or match_label == "mapping_unknown" or "unknown" in row.get("mapping_status", "")


def query_version_for(row: dict[str, str]) -> str:
    version_kind = row.get("version_kind", "")
    resolved = exact_version(row.get("resolved_version", ""), "resolved") if row.get("resolved_version") else ""
    if resolved:
        return resolved
    if version_kind == "range":
        return ""
    return exact_version(row.get("version_spec", ""), version_kind)


def query_package_for(row: dict[str, str]) -> str:
    if is_npm_alias_dependency(row):
        return row.get("namespace_hint", "")
    return row.get("declared_package") or row.get("package_candidate", "")


def go_escape_module(module: str) -> str:
    out = []
    for char in module:
        if "A" <= char <= "Z":
            out.append("!" + char.lower())
        else:
            out.append(char)
    return "".join(out)


def registry_lookup(
    row: dict[str, str],
    cache_dir: Path,
    timeout: int,
    retry_count: int = 0,
    retry_sleep: float = 0.5,
    depsdev_mode: str = "uncertain",
) -> dict[str, Any]:
    ecosystem = row.get("ecosystem", "")
    package = row.get("query_package", "")
    if ecosystem not in SUPPORTED_REGISTRIES:
        return registry_result(row, "unsupported_ecosystem", "", "", "", "", "")
    if not package:
        return registry_result(row, "no_package", "", "", "", "", "")
    if ecosystem == "npm":
        result = npm_lookup(row, cache_dir, timeout, retry_count, retry_sleep)
    elif ecosystem == "PyPI":
        result = pypi_lookup(row, cache_dir, timeout, retry_count, retry_sleep)
    elif ecosystem == "Cargo":
        result = cargo_lookup(row, cache_dir, timeout, retry_count, retry_sleep)
    elif ecosystem == "Go":
        result = go_lookup(row, cache_dir, timeout, retry_count, retry_sleep)
    elif ecosystem == "Maven":
        result = maven_lookup(row, cache_dir, timeout, retry_count, retry_sleep)
    elif ecosystem == "NuGet":
        result = nuget_lookup(row, cache_dir, timeout, retry_count, retry_sleep)
    elif ecosystem == "Packagist":
        result = packagist_lookup(row, cache_dir, timeout, retry_count, retry_sleep)
    else:
        result = registry_result(row, "unsupported_ecosystem", "", "", "", "", "")
    if should_query_depsdev(result, depsdev_mode):
        result = merge_depsdev_result(result, depsdev_lookup(row, cache_dir, timeout, retry_count, retry_sleep))
    return result


def should_query_depsdev(result: dict[str, Any], mode: str) -> bool:
    if mode == "off":
        return False
    if mode == "all":
        return str(result.get("registry_failure_bucket", "")) != "unsupported_ecosystem"
    if result.get("package_exists_current_registry") is False or result.get("version_exists_current_registry") is False:
        return True
    if result.get("version_exists_current_registry") is True and not result.get("publish_time"):
        return True
    return str(result.get("registry_failure_bucket", "")) not in {"ok", "unsupported_ecosystem", "no_package"}


def registry_failure_bucket(status: Any, error: str = "") -> str:
    text = str(status)
    if text == "200":
        return "ok"
    if text in {"unsupported_ecosystem", "no_package", "malformed_package_name"}:
        return text
    if text in {"", "0", "None"}:
        return "network_or_decode_error"
    if text == "400":
        return "bad_request_or_malformed_version"
    if text in {"401", "403"}:
        return "auth_or_forbidden"
    if text == "404":
        return "not_found_current_registry"
    if text == "429":
        return "rate_limited"
    if text in {"408", "409", "425"}:
        return "temporary_registry_error"
    if text.startswith("5"):
        return "registry_server_error"
    return f"http_{text}"


def registry_failure_detail(status: Any, error: str = "") -> str:
    bucket = registry_failure_bucket(status, error)
    if bucket == "ok":
        return "ok"
    lowered = (error or "").lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "name or service not known" in lowered or "getaddrinfo" in lowered or "dns" in lowered:
        return "dns_or_resolution_error"
    if "json" in lowered or "decode" in lowered:
        return "decode_error"
    if "connection reset" in lowered or "remote end closed" in lowered:
        return "connection_closed"
    if bucket == "not_found_current_registry":
        return "http_404_not_found"
    if bucket in {"rate_limited", "temporary_registry_error", "registry_server_error"}:
        return bucket
    return bucket


def registry_result(row: dict[str, str], status: Any, package_exists: Any, version_exists: Any, publish_time: str, error: str, source: str) -> dict[str, Any]:
    return {
        "ecosystem": row.get("ecosystem", ""),
        "package_name": row.get("query_package", ""),
        "version": row.get("query_version", ""),
        "registry_status": status,
        "registry_failure_bucket": registry_failure_bucket(status, error),
        "registry_failure_detail": registry_failure_detail(status, error),
        "package_exists_current_registry": package_exists,
        "version_exists_current_registry": version_exists,
        "publish_time": publish_time,
        "registry_error": error,
        "registry_source": source,
    }


def add_registry_note(result: dict[str, Any], note: str, detail: str = "") -> dict[str, Any]:
    result["registry_note"] = note
    result["registry_note_detail"] = detail
    return result


def depsdev_lookup(row: dict[str, str], cache_dir: Path, timeout: int, retry_count: int = 0, retry_sleep: float = 0.5) -> dict[str, Any]:
    ecosystem = row.get("ecosystem", "")
    system = DEPSDEV_SYSTEMS.get(ecosystem, "")
    package = row.get("query_package", "")
    version = row.get("query_version", "")
    if not system or not package:
        return {"depsdev_status": "unsupported_or_no_package", "depsdev_source": "api.deps.dev"}
    encoded_package = urllib.parse.quote(package, safe="")
    if version:
        encoded_version = urllib.parse.quote(version, safe="")
        url = f"https://api.deps.dev/v3/systems/{system}/packages/{encoded_package}/versions/{encoded_version}"
        status, data, error = cached_http_json(cache_dir / "depsdev", ("depsdev-version", system, package, version), url, timeout, retry_count, retry_sleep)
        if status == 200 and isinstance(data, dict):
            key = data.get("versionKey", {}) if isinstance(data.get("versionKey"), dict) else {}
            return {
                "depsdev_status": status,
                "depsdev_package_exists": True,
                "depsdev_version_exists": True,
                "depsdev_canonical_package": key.get("name", package),
                "depsdev_canonical_version": key.get("version", version),
                "depsdev_published_at": data.get("publishedAt", ""),
                "depsdev_error": error,
                "depsdev_source": "api.deps.dev",
            }
        if status not in {404, "404"}:
            return {"depsdev_status": status, "depsdev_error": error, "depsdev_source": "api.deps.dev"}
    url = f"https://api.deps.dev/v3/systems/{system}/packages/{encoded_package}"
    status, data, error = cached_http_json(cache_dir / "depsdev", ("depsdev-package", system, package), url, timeout, retry_count, retry_sleep)
    versions = data.get("versions", []) if status == 200 and isinstance(data, dict) else []
    version_names = {str((item.get("versionKey") or {}).get("version", "")) for item in versions if isinstance(item, dict)}
    publish_time = ""
    if version:
        for item in versions:
            key = item.get("versionKey", {}) if isinstance(item, dict) else {}
            if key.get("version") == version:
                publish_time = item.get("publishedAt", "")
                break
    return {
        "depsdev_status": status,
        "depsdev_package_exists": status == 200,
        "depsdev_version_exists": bool(version and version in version_names),
        "depsdev_canonical_package": ((data.get("packageKey") or {}).get("name", package) if isinstance(data, dict) else package),
        "depsdev_canonical_version": version,
        "depsdev_published_at": publish_time,
        "depsdev_error": error,
        "depsdev_source": "api.deps.dev",
    }


def merge_depsdev_result(result: dict[str, Any], depsdev: dict[str, Any]) -> dict[str, Any]:
    if not depsdev:
        return result
    merged = {**result, **depsdev}
    if depsdev.get("depsdev_package_exists") is True and result.get("package_exists_current_registry") is False:
        merged["package_exists_current_registry"] = True
        merged["registry_note"] = append_note(str(merged.get("registry_note", "")), "depsdev_corrected_package_exists")
    if depsdev.get("depsdev_version_exists") is True and result.get("version_exists_current_registry") is False:
        merged["version_exists_current_registry"] = True
        merged["registry_note"] = append_note(str(merged.get("registry_note", "")), "depsdev_corrected_version_exists")
    if depsdev.get("depsdev_published_at") and not result.get("publish_time"):
        merged["publish_time"] = depsdev.get("depsdev_published_at")
        merged["registry_note"] = append_note(str(merged.get("registry_note", "")), "depsdev_filled_publish_time")
    if merged.get("version_exists_current_registry") is True or (merged.get("package_exists_current_registry") is True and not result.get("version")):
        merged["registry_failure_bucket"] = "ok"
        merged["registry_failure_detail"] = "ok"
    return merged


def append_note(existing: str, note: str) -> str:
    parts = [part for part in existing.split(";") if part]
    if note not in parts:
        parts.append(note)
    return ";".join(parts)


def compact_npm_data(data: Any) -> Any:
    if not isinstance(data, dict) or data.get("_cache_schema") == "npm-minimal-v1":
        return data
    versions = data.get("versions", {}) if isinstance(data.get("versions", {}), dict) else {}
    names = list(versions)
    times = data.get("time", {}) if isinstance(data.get("time", {}), dict) else {}
    deprecated = {
        version: metadata.get("deprecated", "")
        for version, metadata in versions.items()
        if isinstance(metadata, dict) and metadata.get("deprecated")
    }
    return {
        "_cache_schema": "npm-minimal-v1",
        "name": data.get("name", ""),
        "version_names": names,
        "version_times": {version: times[version] for version in names if version in times},
        "deprecated_versions": deprecated,
        "dist_tags": data.get("dist-tags", {}),
    }


def npm_version_evidence(data: Any) -> tuple[set[str], dict[str, str]]:
    if not isinstance(data, dict):
        return set(), {}
    if data.get("_cache_schema") == "npm-minimal-v1":
        return set(data.get("version_names", [])), data.get("version_times", {}) or {}
    versions = data.get("versions", {}) or {}
    times = data.get("time", {}) or {}
    return set(versions), times

def npm_lookup(row: dict[str, str], cache_dir: Path, timeout: int, retry_count: int = 0, retry_sleep: float = 0.5) -> dict[str, Any]:
    package = row["query_package"]
    version = row.get("query_version", "")
    url = f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='@')}"
    status, data, error = cached_http_json(
        cache_dir / "npm", ("npm", package), url, timeout, retry_count, retry_sleep,
        data_transform=compact_npm_data,
    )
    versions, version_times = npm_version_evidence(data)
    publish_time = version_times.get(version, "") if version else ""
    result = registry_result(row, status, status == 200, bool(version and version in versions), publish_time, error, "registry.npmjs.org")
    if status == 200 and version and version not in versions:
        prefix_matches = npm_partial_semver_matches(version, versions)
        if prefix_matches:
            return add_registry_note(result, "npm_partial_semver_prefix_matches_current_versions", ";".join(prefix_matches[:8]))
    return result


def npm_partial_semver_matches(version: str, versions: Iterable[str]) -> list[str]:
    if not re.fullmatch(r"\d+\.\d+", version):
        return []
    prefix = f"{version}."
    return sorted(item for item in versions if item.startswith(prefix))


def pypi_lookup(row: dict[str, str], cache_dir: Path, timeout: int, retry_count: int = 0, retry_sleep: float = 0.5) -> dict[str, Any]:
    package = normalize_pypi(row["query_package"])
    version = row.get("query_version", "")
    url = f"https://pypi.org/pypi/{urllib.parse.quote(package)}/json"
    status, data, error = cached_http_json(cache_dir / "pypi", ("pypi", package), url, timeout, retry_count, retry_sleep)
    releases = data.get("releases", {}) if isinstance(data, dict) else {}
    files = releases.get(version, []) if version else []
    upload_times = [item.get("upload_time_iso_8601", "") for item in files if item.get("upload_time_iso_8601")]
    publish_time = min(upload_times) if upload_times else ""
    out = registry_result(row, status, status == 200, bool(version and version in releases), publish_time, error, "pypi.org")
    out["package_name"] = package
    return out


def cargo_lookup(row: dict[str, str], cache_dir: Path, timeout: int, retry_count: int = 0, retry_sleep: float = 0.5) -> dict[str, Any]:
    package = row["query_package"]
    version = row.get("query_version", "")
    if version:
        url = f"https://crates.io/api/v1/crates/{urllib.parse.quote(package)}/{urllib.parse.quote(version)}"
        status, data, error = cached_http_json(cache_dir / "cargo", ("cargo", package, version), url, timeout, retry_count, retry_sleep)
        version_data = data.get("version", {}) if isinstance(data, dict) else {}
        return registry_result(row, status, status == 200, status == 200, version_data.get("created_at", ""), error, "crates.io")
    url = f"https://crates.io/api/v1/crates/{urllib.parse.quote(package)}"
    status, _, error = cached_http_json(cache_dir / "cargo", ("cargo", package), url, timeout, retry_count, retry_sleep)
    return registry_result(row, status, status == 200, "", "", error, "crates.io")


def maven_lookup(row: dict[str, str], cache_dir: Path, timeout: int, retry_count: int = 0, retry_sleep: float = 0.5) -> dict[str, Any]:
    package = row["query_package"]
    version = row.get("query_version", "")
    if ":" not in package:
        return registry_result(row, "malformed_package_name", "", "", "", "expected group:artifact", "search.maven.org")
    group, artifact = package.split(":", 1)
    query = f'g:"{group}" AND a:"{artifact}" AND v:"{version}"'
    url = "https://search.maven.org/solrsearch/select?" + urllib.parse.urlencode({"q": query, "rows": "1", "wt": "json"})
    status, data, error = cached_http_json(cache_dir / "maven", ("maven", package, version), url, timeout, retry_count, retry_sleep)
    docs = (((data or {}).get("response") or {}).get("docs") or []) if isinstance(data, dict) else []
    timestamp = docs[0].get("timestamp", "") if docs else ""
    publish_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z") if isinstance(timestamp, (int, float)) else ""
    if docs or status not in {200, "200"}:
        return registry_result(row, status, status == 200, bool(docs), publish_time, error, "search.maven.org")
    google = google_maven_lookup(row, cache_dir, timeout, retry_count, retry_sleep, group, artifact, version)
    if google.get("package_exists_current_registry") or google.get("registry_status") in {200, "200"}:
        return google
    repo1 = repo1_maven_lookup(row, cache_dir, timeout, retry_count, retry_sleep, group, artifact, version)
    if repo1.get("package_exists_current_registry") or repo1.get("registry_status") in {200, "200"}:
        return repo1
    if google.get("registry_failure_bucket") not in {"not_found_current_registry", "ok"}:
        return google
    return registry_result(row, 404, False, False, publish_time, error, "search.maven.org")


def google_maven_lookup(
    row: dict[str, str],
    cache_dir: Path,
    timeout: int,
    retry_count: int,
    retry_sleep: float,
    group: str,
    artifact: str,
    version: str,
) -> dict[str, Any]:
    group_path = "/".join(group.split("."))
    url = f"https://dl.google.com/dl/android/maven2/{group_path}/{artifact}/maven-metadata.xml"
    status, text, error = cached_http_text(cache_dir / "google-maven", ("google-maven", group, artifact), url, timeout, retry_count, retry_sleep)
    if status != 200:
        return registry_result(row, status, status == 200, False, "", error, "dl.google.com/android/maven2")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return registry_result(row, 0, True, "", "", f"google maven metadata parse error: {exc}", "dl.google.com/android/maven2")
    versions = {node.text.strip() for node in root.findall(".//version") if node.text and node.text.strip()}
    version_exists = version in versions if version else False
    return registry_result(row, 200, True, version_exists, "", error, "dl.google.com/android/maven2")


def repo1_maven_lookup(
    row: dict[str, str],
    cache_dir: Path,
    timeout: int,
    retry_count: int,
    retry_sleep: float,
    group: str,
    artifact: str,
    version: str,
) -> dict[str, Any]:
    group_path = "/".join(group.split("."))
    base = f"https://repo1.maven.org/maven2/{group_path}/{artifact}"
    metadata_url = f"{base}/maven-metadata.xml"
    status, text, error = cached_http_text(cache_dir / "repo1-maven", ("repo1-maven-metadata", group, artifact), metadata_url, timeout, retry_count, retry_sleep)
    if status == 200:
        try:
            root = ET.fromstring(text)
            versions = {node.text.strip() for node in root.findall(".//version") if node.text and node.text.strip()}
        except ET.ParseError as exc:
            return registry_result(row, 0, True, "", "", f"repo1 maven metadata parse error: {exc}", "repo1.maven.org/maven2")
        if not version:
            return registry_result(row, 200, True, "", "", error, "repo1.maven.org/maven2")
        if version in versions:
            return registry_result(row, 200, True, True, "", error, "repo1.maven.org/maven2")
        return registry_result(row, 200, True, False, "", error, "repo1.maven.org/maven2")
    if version:
        pom_url = f"{base}/{version}/{artifact}-{version}.pom"
        pom_status, _, pom_error = cached_http_text(cache_dir / "repo1-maven", ("repo1-maven-pom", group, artifact, version), pom_url, timeout, retry_count, retry_sleep)
        if pom_status == 200:
            return registry_result(row, 200, True, True, "", pom_error, "repo1.maven.org/maven2")
        if pom_status not in {404, "404"}:
            return registry_result(row, pom_status, "", "", "", pom_error, "repo1.maven.org/maven2")
    return registry_result(row, status, status == 200, False, "", error, "repo1.maven.org/maven2")


def packagist_lookup(row: dict[str, str], cache_dir: Path, timeout: int, retry_count: int = 0, retry_sleep: float = 0.5) -> dict[str, Any]:
    package = row["query_package"]
    version = row.get("query_version", "")
    encoded = urllib.parse.quote(package, safe="/")
    url = f"https://repo.packagist.org/p2/{encoded}.json"
    status, data, error = cached_http_json(cache_dir / "packagist", ("packagist", package), url, timeout, retry_count, retry_sleep)
    packages = data.get("packages", {}) if isinstance(data, dict) else {}
    versions = packages.get(package, []) if isinstance(packages, dict) else []
    version_exists = False
    publish_time = ""
    normalized = {version, version.removeprefix("v"), f"v{version}"} if version else set()
    for item in versions if isinstance(versions, list) else []:
        item_version = str(item.get("version", ""))
        if item_version in normalized:
            version_exists = True
            publish_time = str(item.get("time", ""))
            break
    return registry_result(row, status, status == 200, version_exists, publish_time, error, "repo.packagist.org")


def nuget_lookup(row: dict[str, str], cache_dir: Path, timeout: int, retry_count: int = 0, retry_sleep: float = 0.5) -> dict[str, Any]:
    package = row["query_package"]
    version = row.get("query_version", "")
    lower = package.lower()
    index_url = f"https://api.nuget.org/v3-flatcontainer/{urllib.parse.quote(lower)}/index.json"
    index_status, index_data, index_error = cached_http_json(cache_dir / "nuget", ("nuget-index", lower), index_url, timeout, retry_count, retry_sleep)
    versions = index_data.get("versions", []) if isinstance(index_data, dict) else []
    package_exists = index_status == 200
    version_exists = version.lower() in {str(item).lower() for item in versions} if version else False
    publish_time = ""
    error = index_error
    status = index_status
    if version_exists:
        leaf_url = f"https://api.nuget.org/v3/registration5-semver2/{urllib.parse.quote(lower)}/{urllib.parse.quote(version.lower())}.json"
        status, data, error = cached_http_json(cache_dir / "nuget", ("nuget", lower, version.lower()), leaf_url, timeout, retry_count, retry_sleep)
        catalog = data.get("catalogEntry", {}) if isinstance(data, dict) else {}
        publish_time = data.get("published", "") if isinstance(data, dict) else ""
        if not publish_time and isinstance(catalog, dict):
            publish_time = catalog.get("published", "")
        if status != 200:
            error = error or "version listed in flat-container but registration leaf unavailable"
    return registry_result(row, status if package_exists else index_status, package_exists, version_exists, publish_time, error, "api.nuget.org")


def go_lookup(row: dict[str, str], cache_dir: Path, timeout: int, retry_count: int = 0, retry_sleep: float = 0.5) -> dict[str, Any]:
    package = row["query_package"]
    version = row.get("query_version", "")
    if not valid_go_module(package):
        return registry_result(row, "malformed_package_name", "", "", "", "invalid Go module path", "proxy.golang.org")
    escaped = go_escape_module(package)
    if version:
        url = f"https://proxy.golang.org/{escaped}/@v/{urllib.parse.quote(version)}.info"
        status, data, error = cached_http_json(cache_dir / "go", ("go", package, version), url, timeout, retry_count, retry_sleep)
        return registry_result(row, status, status == 200, status == 200, data.get("Time", "") if isinstance(data, dict) else "", error, "proxy.golang.org")
    url = f"https://proxy.golang.org/{escaped}/@v/list"
    status, _, error = cached_http_json(cache_dir / "go", ("go", package), url, timeout, retry_count, retry_sleep)
    return registry_result(row, status, status == 200, "", "", error, "proxy.golang.org")


def valid_go_module(package: str) -> bool:
    if not package or any(char.isspace() for char in package):
        return False
    if any(char in package for char in "${}()[]<>'\""):
        return False
    return "/" in package and "." in package.split("/")[0]


BOM_RESULT_FIELDS = [
    "repo",
    "sha",
    "source_file",
    "declared_package",
    "version_before",
    "bom_alias",
    "bom_package",
    "bom_version",
    "bom_source",
    "fetch_status",
    "resolved_version",
    "evidence",
]


def enrich_bom_managed_versions(
    match_rows: list[dict[str, str]],
    cache_dir: Path,
    timeout: int,
    retry_count: int = 0,
    retry_sleep: float = 0.5,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    rows: list[dict[str, str]] = []
    audit_rows: list[dict[str, Any]] = []
    bom_cache: dict[tuple[str, str], tuple[str, str, dict[str, str], str]] = {}
    for row in match_rows:
        if not is_bom_managed_dependency(row):
            rows.append(row)
            continue
        info = parse_bom_management_info(row.get("dependency_group", ""))
        target = row.get("declared_package") or row.get("package_candidate", "")
        if not info or ":" not in target:
            rows.append(row)
            audit_rows.append(bom_audit_row(row, info, "", "not_applicable", "", "missing_bom_or_target_coordinates"))
            continue
        key = (info["package"], info["version"])
        if key not in bom_cache:
            bom_cache[key] = fetch_parse_bom_versions(info["package"], info["version"], cache_dir, timeout, retry_count, retry_sleep)
        source, status, versions, evidence = bom_cache[key]
        resolved = versions.get(target, "")
        audit_rows.append(bom_audit_row(row, info, source, status, resolved, evidence if resolved else f"{evidence};target_not_listed"))
        if resolved:
            updated = dict(row)
            updated["version_spec"] = resolved
            updated["version_kind"] = "exact"
            updated["resolved_version"] = resolved
            updated["resolution_source"] = append_note(str(updated.get("resolution_source", "")), "gradle_bom_dependency_management")
            updated["dependency_group"] = append_note(str(updated.get("dependency_group", "")), f"bom_resolved_from:{source}")
            rows.append(updated)
        else:
            rows.append(row)
    return rows, audit_rows


def parse_bom_management_info(group: str) -> dict[str, str] | None:
    for part in str(group or "").split(";"):
        if not part.startswith("bom_managed_by:"):
            continue
        values = part.removeprefix("bom_managed_by:").split(":", 3)
        if len(values) != 4:
            return None
        alias, group_id, artifact_id, version = values
        if not group_id or not artifact_id or not version:
            return None
        return {"alias": alias, "package": f"{group_id}:{artifact_id}", "version": version}
    return None


def fetch_parse_bom_versions(
    package: str,
    version: str,
    cache_dir: Path,
    timeout: int,
    retry_count: int,
    retry_sleep: float,
) -> tuple[str, str, dict[str, str], str]:
    if ":" not in package or not version:
        return "", "not_applicable", {}, "malformed_bom_coordinates"
    group, artifact = package.split(":", 1)
    sources = [
        ("dl.google.com/android/maven2", google_maven_pom_url(group, artifact, version)),
        ("repo1.maven.org", repo1_maven_pom_url(group, artifact, version)),
    ]
    errors = []
    for source, url in sources:
        status, text, error = cached_http_text(cache_dir / "bom-pom", ("bom-pom", source, group, artifact, version), url, timeout, retry_count, retry_sleep)
        if status != 200:
            errors.append(f"{source}:{status}")
            continue
        versions, parse_error = parse_bom_dependency_management(text)
        if parse_error:
            return source, "parse_error", {}, parse_error
        return source, "ok", versions, f"dependency_management_entries={len(versions)}"
    return "", "not_found_or_fetch_failed", {}, ";".join(errors)


def google_maven_pom_url(group: str, artifact: str, version: str) -> str:
    group_path = "/".join(group.split("."))
    return f"https://dl.google.com/dl/android/maven2/{group_path}/{artifact}/{version}/{artifact}-{version}.pom"


def repo1_maven_pom_url(group: str, artifact: str, version: str) -> str:
    group_path = "/".join(group.split("."))
    return f"https://repo1.maven.org/maven2/{group_path}/{artifact}/{version}/{artifact}-{version}.pom"


def parse_bom_dependency_management(text: str) -> tuple[dict[str, str], str]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {}, f"bom_pom_parse_error:{exc}"
    properties = collect_pom_properties(root)
    versions: dict[str, str] = {}
    for management in root.findall(".//{*}dependencyManagement"):
        deps = direct_child(management, "dependencies")
        if deps is None:
            continue
        for dep in list(deps):
            if local_name(dep.tag) != "dependency":
                continue
            group = resolve_pom_value(child_text(dep, "groupId"), properties)
            artifact = resolve_pom_value(child_text(dep, "artifactId"), properties)
            version = resolve_pom_value(child_text(dep, "version"), properties)
            if group and artifact and exact_version(version):
                versions[f"{group}:{artifact}"] = exact_version(version)
    return versions, ""


def collect_pom_properties(root: ET.Element) -> dict[str, str]:
    props: dict[str, str] = {}
    group = child_text(root, "groupId")
    artifact = child_text(root, "artifactId")
    version = child_text(root, "version")
    if group:
        props["project.groupId"] = group
        props["pom.groupId"] = group
    if artifact:
        props["project.artifactId"] = artifact
        props["pom.artifactId"] = artifact
    if version:
        props["project.version"] = version
        props["pom.version"] = version
    properties_node = direct_child(root, "properties")
    if properties_node is not None:
        for child in list(properties_node):
            value = (child.text or "").strip()
            if value:
                props[local_name(child.tag)] = value
    return props


def resolve_pom_value(value: str, properties: dict[str, str]) -> str:
    out = (value or "").strip()
    for _ in range(5):
        changed = False
        for match in re.findall(r"\$\{([^}]+)\}", out):
            if match in properties:
                out = out.replace("${" + match + "}", properties[match])
                changed = True
        if not changed:
            break
    return out if "${" not in out else ""


def direct_child(node: ET.Element, name: str) -> ET.Element | None:
    for child in list(node):
        if local_name(child.tag) == name:
            return child
    return None


def child_text(node: ET.Element, name: str) -> str:
    child = direct_child(node, name)
    return (child.text or "").strip() if child is not None else ""


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def bom_audit_row(row: dict[str, str], info: dict[str, str] | None, source: str, status: str, resolved: str, evidence: str) -> dict[str, Any]:
    return {
        "repo": row.get("repo", ""),
        "sha": row.get("sha", ""),
        "source_file": row.get("source_file", ""),
        "declared_package": row.get("declared_package") or row.get("package_candidate", ""),
        "version_before": row.get("version_spec", ""),
        "bom_alias": (info or {}).get("alias", ""),
        "bom_package": (info or {}).get("package", ""),
        "bom_version": (info or {}).get("version", ""),
        "bom_source": source,
        "fetch_status": status,
        "resolved_version": resolved,
        "evidence": evidence,
    }


def build_query_rows(match_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = []
    seen = set()
    for row in match_rows:
        package = query_package_for(row)
        version_kind = row.get("version_kind", "")
        if is_mapping_uncertain(row) or is_private_or_local_spec(row.get("version_spec", ""), version_kind) or is_first_party_dependency(row) or is_gradle_catalog_possible_dependency(row):
            continue
        if is_maven_snapshot_version(row) or is_npm_custom_registry_dependency(row):
            continue
        version = query_version_for(row)
        if not version:
            continue
        item = {**row, "query_package": package, "query_version": version}
        key = (item.get("ecosystem", ""), package.lower(), version)
        if key not in seen:
            rows.append(item)
            seen.add(key)
    return rows


def osv_lookup(
    rows: list[dict[str, str]],
    timeout: int,
    batch_size: int,
    sleep: float,
    output_dir: Path | None = None,
    checkpoint_every_batches: int = 0,
    registry_rows: list[dict[str, Any]] | None = None,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    eligible = [row for row in rows if row.get("ecosystem") in OSV_ECOSYSTEMS and row.get("query_package") and row.get("query_version")]
    output: dict[tuple[str, str, str], dict[str, Any]] = {}
    batch_index = 0
    for start in range(0, len(eligible), batch_size):
        chunk = eligible[start:start + batch_size]
        batch_index += 1
        queries = [
            {"package": {"name": row["query_package"], "ecosystem": OSV_ECOSYSTEMS[row["ecosystem"]]}, "version": row["query_version"]}
            for row in chunk
        ]
        status, data, error = http_json("https://api.osv.dev/v1/querybatch", timeout, method="POST", payload={"queries": queries})
        results = data.get("results", []) if status == 200 and isinstance(data, dict) else []
        for index, row in enumerate(chunk):
            key = (row["ecosystem"], row["query_package"], row["query_version"])
            vulns = (results[index] or {}).get("vulns", []) if index < len(results) else []
            output[key] = summarize_vulns(status, vulns, error)
        if output_dir and checkpoint_every_batches and batch_index % checkpoint_every_batches == 0:
            advisory_rows = advisory_rows_from_map(output)
            write_csv(output_dir / "advisory_lookup_results.csv", advisory_rows)
            write_stage3_checkpoint(output_dir, "advisory_lookup", registry_rows or [], advisory_rows, complete=False)
            print(f"stage3_advisory_checkpoint batches={batch_index} advisory_rows={len(advisory_rows)} output_dir={output_dir}", flush=True)
        time.sleep(sleep)
    return output


def summarize_vulns(status: int, vulns: list[dict[str, Any]], error: str) -> dict[str, Any]:
    ids = []
    published = []
    malicious = []
    for vuln in vulns or []:
        vuln_id = str(vuln.get("id", ""))
        if vuln_id:
            ids.append(vuln_id)
        if vuln.get("published"):
            published.append(str(vuln["published"]))
        if vuln_id.startswith("MAL-") or "malicious" in json.dumps(vuln, ensure_ascii=False).lower():
            malicious.append(vuln_id)
    return {
        "osv_status": status,
        "osv_error": error,
        "advisory_count": len(vulns or []),
        "advisory_ids": ";".join(ids),
        "malicious_advisory_ids": ";".join(malicious),
        "earliest_advisory_published": min(published) if published else "",
    }


def build_final_rows(match_rows: list[dict[str, str]], registry: dict[tuple[str, str, str], dict[str, Any]], advisories: dict[tuple[str, str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in match_rows:
        query_package = query_package_for(row)
        query_version = query_version_for(row)
        enriched = {**row, "query_package": query_package, "query_version": query_version}
        reg = registry.get((row.get("ecosystem", ""), query_package.lower(), query_version), {})
        if not reg:
            reg = registry.get((row.get("ecosystem", ""), query_package, query_version), {})
        adv = advisories.get((row.get("ecosystem", ""), query_package, query_version), {})
        label, reason, strength = assign_label(enriched, reg, adv)
        cannot_reason = reason if label == "cannot_compare" else ""
        rows.append({
            **enriched,
            **prefix("registry_", reg),
            **prefix("advisory_", adv),
            "final_label": label,
            "label_reason": reason,
            "cannot_compare_reason": cannot_reason,
            "evidence_strength": strength,
        })
    return rows


def prefix(name: str, values: dict[str, Any]) -> dict[str, Any]:
    return {f"{name}{key}": value for key, value in values.items()}


def assign_label(row: dict[str, str], reg: dict[str, Any], adv: dict[str, Any]) -> tuple[str, str, str]:
    match_label = row.get("declared_dependency_match", "")
    mapping_status = row.get("mapping_status", "")
    ecosystem = row.get("ecosystem", "")
    version_kind = row.get("version_kind", "")
    spec = row.get("version_spec", "")
    author_date = parse_time(row.get("author_date", ""))
    publish_time = parse_time(str(reg.get("publish_time", "")))
    advisory_time = parse_time(str(adv.get("earliest_advisory_published", "")))
    if not row.get("query_package") or is_mapping_uncertain(row):
        return "cannot_compare", "mapping_uncertain", "weak"
    if ecosystem not in SUPPORTED_REGISTRIES:
        return "cannot_compare", "unsupported_ecosystem", "weak"
    if is_first_party_dependency(row):
        return "cannot_compare", "first_party_module_import", "strong_exclusion"
    if is_private_or_local_spec(spec, version_kind):
        return "cannot_compare", "private_or_local_dependency", "strong_exclusion"
    if is_gradle_catalog_possible_dependency(row):
        return "cannot_compare", "catalog_possible_dependency_not_build_usage", "weak"
    if ecosystem == "Go" and row.get("query_version") == "v0.0.0":
        return "cannot_compare", "go_placeholder_version", "strong_exclusion"
    if is_maven_snapshot_version(row):
        return "cannot_compare", "maven_snapshot_or_non_central_version", "weak"
    if is_npm_custom_registry_dependency(row):
        return "cannot_compare", "npm_custom_registry_not_queried", "weak"
    if match_label.startswith("cannot_compare_no_parsed_dependencies"):
        return "cannot_compare", "no_manifest_or_no_parsed_dependencies", "weak"
    registry_status = reg.get("registry_status")
    failure_bucket = str(reg.get("registry_failure_bucket", ""))
    if registry_status in {"unsupported_ecosystem", "no_package", "malformed_package_name"} or registry_status in (0, "0"):
        return "cannot_compare", f"registry_{failure_bucket or 'lookup_failed'}", "weak"
    if failure_bucket in {"bad_request_or_malformed_version", "auth_or_forbidden", "rate_limited", "temporary_registry_error", "registry_server_error", "network_or_decode_error"}:
        detail = str(reg.get("registry_failure_detail", ""))
        suffix = f"_{detail}" if detail and detail != failure_bucket and detail != "ok" else ""
        return "cannot_compare", f"registry_{failure_bucket}{suffix}", "weak"
    if match_label == "not_observed_in_parsed_dependency_files":
        return "not_observed_in_parsed_dependency_files", "manifest_does_not_explain_import", "medium"
    if not row.get("query_version"):
        if is_bom_managed_dependency(row):
            return "cannot_compare", "bom_managed_version_not_resolved", "weak"
        if version_kind == "range":
            return "cannot_compare", "no_exact_version_range", "weak"
        if version_kind == "unresolved":
            return "cannot_compare", "no_declared_or_resolved_version", "weak"
        return "cannot_compare", "no_resolved_version", "weak"
    if reg.get("registry_note") == "npm_partial_semver_prefix_matches_current_versions":
        return "cannot_compare", "no_exact_version_range", "weak"
    if ecosystem == "Packagist" and is_packagist_non_exact_version(row.get("query_version", "")):
        return "cannot_compare", "no_exact_version_range", "weak"
    if ecosystem == "Maven" and (reg.get("package_exists_current_registry") is False or reg.get("version_exists_current_registry") is False):
        return "cannot_compare", "maven_default_registry_missing_custom_repository_possible", "weak"
    current_registry_missing = reg.get("package_exists_current_registry") is False or reg.get("version_exists_current_registry") is False
    if current_registry_missing and dependency_not_introduced_in_commit(row):
        return "cannot_compare", "dependency_not_introduced_in_commit", "strong_exclusion"
    if reg.get("package_exists_current_registry") is False:
        return "unknown_deleted_or_unpublished_possible", "current_registry_package_or_version_missing_no_history", "weak"
    if reg.get("version_exists_current_registry") is False:
        return "unknown_deleted_or_unpublished_possible", "current_registry_version_missing_no_history", "weak"
    if publish_time and author_date and publish_time > author_date:
        return (
            "version_published_after_author_date_candidate",
            "publish_time_after_author_date_requires_history_validation",
            "candidate",
        )
    if adv.get("malicious_advisory_ids") and advisory_time and author_date and advisory_time <= author_date:
        return "known_malicious_package_before_author_date", "malicious_advisory_published_before_commit", "strong"
    if int(adv.get("advisory_count") or 0) > 0 and advisory_time and author_date and advisory_time <= author_date:
        return "known_public_advisory_before_author_date", "advisory_published_before_commit", "strong"
    if publish_time and author_date and publish_time <= author_date:
        return "version_exists_before_author_date", "publish_time_before_or_equal_commit_author_date", "strong"
    return "cannot_compare", "publish_time_unknown", "weak"


def is_packagist_non_exact_version(version: str) -> bool:
    value = (version or "").strip().lower()
    return value.startswith("dev-") or value.endswith("-dev") or "x-dev" in value


def write_report(path: Path, final_rows: list[dict[str, Any]], registry_rows: list[dict[str, Any]], advisory_rows: list[dict[str, Any]], input_rows: int, max_rows: int, input_dir: Path) -> None:
    labels = Counter(str(row.get("final_label", "")) for row in final_rows)
    reasons = Counter(str(row.get("cannot_compare_reason", "")) for row in final_rows if row.get("cannot_compare_reason"))
    failure_buckets = Counter(str(row.get("registry_failure_bucket", "")) for row in registry_rows)
    lines = [
        "# Hallucinated Dependencies End-to-End Report",
        "",
        f"- Input match rows: {input_rows:,}",
        f"- Final label rows: {len(final_rows):,}",
        f"- Registry lookup rows: {len(registry_rows):,}",
        f"- Advisory lookup rows: {len(advisory_rows):,}",
        f"- Max rows limit: {max_rows if max_rows else 'none'}",
        f"- Input dir: `{input_dir}`",
        "",
        "## Final Labels",
        "",
        "| Label | Rows |",
        "|---|---:|",
    ]
    for label, count in labels.most_common():
        lines.append(f"| `{label}` | {count:,} |")
    lines.extend(["", "## Cannot Compare Reasons", "", "| Reason | Rows |", "|---|---:|"])
    for reason, count in reasons.most_common():
        lines.append(f"| `{reason}` | {count:,} |")
    failure_details = Counter(str(row.get("registry_failure_detail", "")) for row in registry_rows)
    lines.extend(["", "## Registry Failure Buckets", "", "| Bucket | Rows |", "|---|---:|"])
    for bucket, count in failure_buckets.most_common():
        lines.append(f"| `{bucket or 'not_queried'}` | {count:,} |")
    lines.extend(["", "## Registry Failure Details", "", "| Detail | Rows |", "|---|---:|"])
    for detail, count in failure_details.most_common():
        lines.append(f"| `{detail or 'not_queried'}` | {count:,} |")
    lines.extend([
        "",
        "## Interpretation Guardrails",
        "",
        "- `unknown_deleted_or_unpublished_possible` is not equal to slopsquatting without historical registry evidence.",
        "- `version_published_after_author_date_candidate` requires committer, parent, root-history, first-party, alias, and human validation; it is not a strong hallucination conclusion.",
        "- Exact or lockfile-resolved versions provide the main commit-time evidence.",
        "- Range-only and mapping-unknown cases stay in `cannot_compare`.",
        "- OSV no-result is not a safety claim; it only means no advisory was returned by the queried source.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir or args.input_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    all_match_rows = read_csv(args.input_dir / "import_dependency_matches.csv")
    input_row_count = len(all_match_rows)
    match_rows = all_match_rows[: args.max_rows] if args.max_rows else all_match_rows
    match_rows, bom_rows = enrich_bom_managed_versions(match_rows, args.cache_dir, args.timeout, args.retry_count, args.retry_sleep_seconds)
    write_csv(output_dir / "bom_dependency_management_results.csv", bom_rows, BOM_RESULT_FIELDS)
    write_stage3_checkpoint(output_dir, "bom_enriched", [], [], complete=False)
    query_rows = build_query_rows(match_rows)
    registry_rows = []
    registry_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    registry_every = max(0, int(args.checkpoint_every_registry_rows or 0))
    for index, row in enumerate(query_rows, 1):
        result = registry_lookup(row, args.cache_dir, args.timeout, args.retry_count, args.retry_sleep_seconds, args.depsdev_mode)
        key = (row.get("ecosystem", ""), row.get("query_package", ""), row.get("query_version", ""))
        registry_map[key] = result
        registry_map[(key[0], key[1].lower(), key[2])] = result
        registry_rows.append(result)
        if registry_every and index % registry_every == 0:
            write_stage3_registry_outputs(output_dir, bom_rows, registry_rows)
            write_stage3_checkpoint(output_dir, "registry_lookup", registry_rows, [], complete=False)
            print(f"stage3_registry_checkpoint rows={len(registry_rows)} output_dir={output_dir}", flush=True)
        time.sleep(args.sleep_seconds)
    write_stage3_registry_outputs(output_dir, bom_rows, registry_rows)
    write_stage3_checkpoint(output_dir, "registry_lookup_complete", registry_rows, [], complete=False)
    advisory_map = osv_lookup(
        query_rows,
        args.timeout,
        args.osv_batch_size,
        args.sleep_seconds,
        output_dir=output_dir,
        checkpoint_every_batches=max(0, int(args.checkpoint_every_osv_batches or 0)),
        registry_rows=registry_rows,
    )
    advisory_rows = advisory_rows_from_map(advisory_map)
    final_rows = build_final_rows(match_rows, registry_map, advisory_map)
    write_stage3_registry_outputs(output_dir, bom_rows, registry_rows)
    write_csv(output_dir / "advisory_lookup_results.csv", advisory_rows)
    write_csv(output_dir / "final_labels.csv", final_rows)
    write_stage3_checkpoint(output_dir, "complete", registry_rows, advisory_rows, final_rows, complete=True)
    write_report(output_dir / "hallucination_pipeline_report.md", final_rows, registry_rows, advisory_rows, input_row_count, args.max_rows, args.input_dir)
    print(f"registry_lookup_rows={len(registry_rows)}")
    print(f"bom_dependency_management_rows={len(bom_rows)}")
    print(f"advisory_lookup_rows={len(advisory_rows)}")
    print(f"final_label_rows={len(final_rows)}")
    print(f"output_dir={output_dir}")


if __name__ == "__main__":
    main()

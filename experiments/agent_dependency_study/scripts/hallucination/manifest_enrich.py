"""Fetch child manifest/lockfile snapshots and match imports to dependencies.

[IN]: candidate_commits.csv and import_events.csv from hallucinated_dependency_probe.py.
[OUT]: manifest_fetch_results.csv, parsed_dependencies.csv, import_dependency_matches.csv, maven_managed_version_results.csv, python_wheel_import_map.csv, python_local_module_map.csv, jvm_local_package_map.csv,
and the shared gzip/content-addressed THESIS_DATA_ROOT/cache/rq2_hallucinated cache.
[POS]: Stage2 child snapshot enrichment and import-to-dependency evidence matching for the RQ3 hallucinated-dependency pipeline.
[SYNC]: If request retry/backoff, manifest parsing, streaming checkpoint
semantics, local-module classification, matching labels, or output schemas change, update
scripts/CLAUDE.md, scripts/OUTPUTS.md, and the active repair plan.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import json
import os
import socket
import sys
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from cache_storage import (
    read_cache_text,
    write_content_addressed_text,
    write_gzip_text,
)
from stage2_storage import (
    CsvSpec,
    OrderedCsvGroupCursor,
    Stage2AppendStore,
    iter_csv_rows,
    stream_candidate_batches,
    stream_grouped_outputs,
)


def raise_csv_field_limit() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


raise_csv_field_limit()

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None


DEFAULT_INPUT = Path("data_products/rq2_hallucinated_mainline_random_smoke")
DEFAULT_OUTPUT = Path("data_products/rq2_hallucinated_manifest_enriched")
DATA_ROOT = Path(os.environ.get("THESIS_DATA_ROOT", r"D:\MasterThesis\thesis-work-data"))
DEFAULT_CACHE = DATA_ROOT / "cache" / "rq2_hallucinated"


def is_dns_resolution_error(error: BaseException) -> bool:
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, socket.gaierror):
            return True
        if getattr(current, "errno", None) == 11001:
            return True
        if getattr(current, "winerror", None) == 11001:
            return True
        current = getattr(current, "reason", None) or current.__cause__
    return "getaddrinfo failed" in str(error).lower()


def dns_outage_sleep_seconds(attempt: int, transient_retries: int) -> int:
    base = max(1, int(os.environ.get('RQ3_DNS_RETRY_BASE_SECONDS', '30')))
    maximum = max(1, int(os.environ.get('RQ3_DNS_RETRY_MAX_SECONDS', '300')))
    outage_attempt = max(1, attempt - transient_retries)
    exponent = min(outage_attempt - 1, 20)
    return min(maximum, base * (2 ** exponent))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--max-commits", type=int, default=0)
    p.add_argument("--fetch-tree", action="store_true")
    p.add_argument("--tree-mode", choices=["fallback-only", "merge-tree"], default="fallback-only")
    p.add_argument("--tree-max-files", type=int, default=24)
    p.add_argument("--sleep-seconds", type=float, default=0.0)
    p.add_argument("--github-token-env", default="GITHUB_TOKEN")
    p.add_argument("--python-wheel-map", choices=["off", "exact"], default="exact")
    p.add_argument("--wheel-size-limit-mb", type=float, default=20.0)
    p.add_argument("--wheel-sleep-seconds", type=float, default=0.05)
    p.add_argument("--checkpoint-every-commits", type=int, default=100,
                   help="Write Stage2 CSV checkpoints every N candidate commits; 0 disables mid-run checkpoints.")
    p.add_argument("--checkpoint-max-buffer-rows", type=int, default=100_000,
                   help="Flush Stage2 append batches after this many buffered rows even before the commit interval; 0 disables the row cap.")
    p.add_argument("--checkpoint-every-wheel-packages", type=int, default=50,
                   help="Flush the Python wheel import-map cache every N newly fetched package versions; 0 disables mid-run cache flushes.")
    p.add_argument("--resume-stage2", dest="resume_stage2", action="store_true", default=True,
                   help="Resume incomplete Stage2 checkpoints from output-dir when possible.")
    p.add_argument("--no-resume-stage2", dest="resume_stage2", action="store_false",
                   help="Ignore incomplete Stage2 checkpoints and rebuild Stage2 from scratch.")
    return p.parse_args()


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


FETCH_FIELDS = ["repo", "sha", "child_sha", "repo_language", "manifest_path", "role", "fetch_status", "discovered_by", "evidence_path_or_url", "error"]
DEP_FIELDS = ["repo", "sha", "repo_language", "author_date", "dep_file_path", "package_name", "version_spec", "resolved_version", "resolution_source", "version_kind", "dependency_group", "namespace_hint", "dependency_file_changed_in_diff"]
WHEEL_FIELDS = ["repo", "sha", "package_name", "version", "import_root", "status", "source", "wheel_filename", "wheel_size", "error"]
PY_LOCAL_FIELDS = ["repo", "sha", "import_root", "source_paths", "status"]
JVM_LOCAL_FIELDS = ["repo", "sha", "package_root", "source_paths", "status"]
IMPORT_FIELDS = [
    "repo", "sha", "author_date", "agent", "repo_language", "ecosystem",
    "source_file", "import_raw", "import_root", "package_candidate",
    "import_kind", "mapping_status",
]
MATCH_EVIDENCE_FIELDS = [
    "declared_dependency_match", "declared_package", "version_spec",
    "resolved_version", "resolution_source", "version_kind",
    "dependency_group", "dep_file_path", "namespace_hint",
    "dependency_file_changed_in_diff",
]


def stage2_fetch_specs() -> dict[str, CsvSpec]:
    return {
        "fetch": CsvSpec("manifest_fetch_results.csv", tuple(FETCH_FIELDS), "manifest_fetch_rows"),
        "deps": CsvSpec("parsed_dependencies.csv", tuple(DEP_FIELDS), "parsed_dependencies"),
        "maven": CsvSpec("maven_managed_version_results.csv", tuple(MAVEN_MANAGED_FIELDS), "maven_managed_version_rows"),
    }


def stage2_match_specs() -> dict[str, CsvSpec]:
    return {
        "matches": CsvSpec("import_dependency_matches.csv", tuple(IMPORT_FIELDS + MATCH_EVIDENCE_FIELDS), "import_matches"),
        "wheel": CsvSpec("python_wheel_import_map.csv", tuple(WHEEL_FIELDS), "python_wheel_import_map_rows"),
        "python_local": CsvSpec("python_local_module_map.csv", tuple(PY_LOCAL_FIELDS), "python_local_module_rows"),
        "jvm_local": CsvSpec("jvm_local_package_map.csv", tuple(JVM_LOCAL_FIELDS), "jvm_local_package_rows"),
    }


def load_token(env_name: str) -> str:
    if os.environ.get(env_name):
        return os.environ[env_name]
    env_file = Path("scripts/.env")
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.strip() == env_name:
            return value.strip().strip('"').strip("'")
    return ""


def rate_limit_sleep_seconds(headers: object, body: str, status: int) -> int:
    get = getattr(headers, "get", lambda _key, _default="": _default)
    retry_after = str(get("Retry-After", "") or "").strip()
    if retry_after.isdigit():
        return bounded_sleep_seconds(int(retry_after) + 5)
    lowered = (body or "").lower()
    reset = str(get("X-RateLimit-Reset", "") or "").strip()
    remaining = str(get("X-RateLimit-Remaining", "") or "").strip()
    if reset.isdigit() and (remaining == "0" or "rate limit" in lowered):
        return bounded_sleep_seconds(max(0, int(reset) - int(time.time()) + 5))
    if status == 429 or "secondary rate limit" in lowered or "rate limit exceeded" in lowered:
        return bounded_sleep_seconds(65)
    return 0


def bounded_sleep_seconds(value: int) -> int:
    max_sleep = int(os.environ.get("RQ3_RATE_LIMIT_MAX_SLEEP_SECONDS", "3900"))
    return max(1, min(value, max_sleep))


def response_rate_sleep_seconds(headers: object) -> int:
    get = getattr(headers, "get", lambda _key, _default="": _default)
    remaining = str(get("X-RateLimit-Remaining", "") or "").strip()
    reset = str(get("X-RateLimit-Reset", "") or "").strip()
    if remaining == "0" and reset.isdigit():
        return bounded_sleep_seconds(max(0, int(reset) - int(time.time()) + 5))
    return 0


def request_text(url: str, token: str) -> tuple[int, str, str]:
    headers = {"User-Agent": "rq3-hallucinated-dependency-probe"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    transient_retries = int(os.environ.get("RQ3_REQUEST_TRANSIENT_RETRIES", "3"))
    attempt = 0
    while True:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                text = response.read().decode("utf-8", errors="replace")
                if attempt > transient_retries:
                    print(
                        f"request_dns_recovered attempts={attempt} url={url}",
                        flush=True,
                    )
                sleep_seconds = response_rate_sleep_seconds(response.headers)
                if sleep_seconds:
                    print(f"rate_limit_wait remaining=0 sleep_seconds={sleep_seconds} url={url}", flush=True)
                    time.sleep(sleep_seconds)
                return response.status, text, ""
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            sleep_seconds = rate_limit_sleep_seconds(exc.headers, body, exc.code)
            if exc.code in {403, 429} and sleep_seconds:
                print(f"request_limited status={exc.code} sleep_seconds={sleep_seconds} url={url}", flush=True)
                time.sleep(sleep_seconds)
                continue
            return exc.code, "", body or str(exc)
        except (OSError, http.client.IncompleteRead) as exc:
            attempt += 1
            if attempt <= transient_retries:
                sleep_seconds = min(60, 2 * attempt)
                print(f"request_transient_error attempt={attempt} sleep_seconds={sleep_seconds} error={exc} url={url}", flush=True)
                time.sleep(sleep_seconds)
                continue
            if is_dns_resolution_error(exc):
                sleep_seconds = dns_outage_sleep_seconds(
                    attempt, transient_retries,
                )
                outage_attempt = attempt - transient_retries
                print(
                    "request_dns_outage_wait "
                    f"attempt={attempt} outage_attempt={outage_attempt} "
                    f"sleep_seconds={sleep_seconds} error={exc} url={url}",
                    flush=True,
                )
                time.sleep(sleep_seconds)
                continue
            return 0, "", str(exc)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "_"


def manifest_cache_target(cache_dir: Path, repo: str, sha: str, path: str) -> Path:
    target = cache_dir / "manifests" / safe_name(repo) / sha / safe_name(path)
    if len(str(target.absolute())) + len(".gz") <= 240:
        return target
    repo_digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:24]
    path_digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    raw_suffix = "".join(Path(path).suffixes[-2:])
    suffix = safe_name(raw_suffix).lstrip(".")[-20:] if raw_suffix else ""
    filename = f"{path_digest}.{suffix}" if suffix else path_digest
    return cache_dir / "manifests" / "_long" / repo_digest / sha / filename


def manifest_candidates(language: str) -> tuple[set[str], set[str]]:
    if language in {"JavaScript", "TypeScript", "TSX", "Vue", "Svelte", "Astro"}:
        return {"package.json"}, {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json"}
    if language == "Python":
        return {"requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "pipfile"}, {"poetry.lock", "pipfile.lock", "uv.lock"}
    if language == "Rust":
        return {"cargo.toml"}, {"cargo.lock"}
    if language == "Go":
        return {"go.mod"}, {"go.sum"}
    if language in {"Java", "Kotlin", "Scala", "Groovy", "Clojure"}:
        return {"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "libs.versions.toml"}, {"gradle.lockfile"}
    if language == "PHP":
        return {"composer.json"}, {"composer.lock"}
    if language in {"C#", "F#"}:
        return {"packages.config", "paket.dependencies", "directory.packages.props"}, {"packages.lock.json", "paket.lock"}
    if language in {"C", "C++", "C_C++_Header", "Objective-C"}:
        return {"vcpkg.json", "conanfile.txt", "conanfile.py"}, {"vcpkg-lock.json", "conan.lock"}
    return set(), set()


def file_role(path: str, language: str) -> str:
    manifests, locks = manifest_candidates(language)
    base = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    if base in manifests or (language in {"C#", "F#"} and suffix in {".csproj", ".fsproj", ".vbproj"}):
        return "manifest"
    if base in locks:
        return "lockfile"
    return "other"


def is_candidate_file(path: str, language: str) -> bool:
    manifests, locks = manifest_candidates(language)
    base = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    if language in {"C#", "F#"} and suffix in {".csproj", ".fsproj", ".vbproj"}:
        return True
    return base in manifests | locks


def load_commit_json(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}


def changed_manifest_files(candidate: dict[str, str]) -> list[dict[str, str]]:
    data = load_commit_json(candidate.get("json_path", ""))
    language = candidate.get("repo_language", "")
    rows = []
    for item in data.get("files", []) or []:
        filename = str(item.get("filename", ""))
        if is_candidate_file(filename, language):
            rows.append({"path": filename, "role": file_role(filename, language), "discovered_by": "changed_files", "raw_url": str(item.get("raw_url", ""))})
    return rows


def raw_url(repo: str, sha: str, path: str) -> str:
    encoded = "/".join(urllib.parse.quote(part) for part in path.split("/"))
    return f"https://raw.githubusercontent.com/{repo}/{sha}/{encoded}"


def fetch_cached(url: str, token: str, cache_dir: Path, repo: str, sha: str, path: str) -> tuple[str, str, str]:
    target = manifest_cache_target(cache_dir, repo, sha, path)
    cached, actual_path = read_cache_text(target)
    if cached is not None:
        return "ok", cached, str(actual_path)
    status, text, error = request_text(url, token)
    if status == 200:
        actual_path = write_content_addressed_text(
            target, text, cache_dir / "manifest_blobs",
        )
        return "ok", text, str(actual_path)
    return f"error_{status}", "", error


def fetch_file(file_row: dict[str, str], candidate: dict[str, str], token: str, cache_dir: Path) -> tuple[str, str, str]:
    url = file_row.get("raw_url") or raw_url(candidate["repo"], candidate["sha"], file_row["path"])
    return fetch_cached(url, token, cache_dir, candidate["repo"], candidate["sha"], file_row["path"])


def tree_manifest_files(
    candidate: dict[str, str], token: str, cache_dir: Path, max_files: int = 24,
    import_packages: set[str] | None = None,
) -> list[dict[str, str]]:
    repo = candidate.get("repo", "")
    sha = candidate.get("sha", "")
    url = f"https://api.github.com/repos/{repo}/git/trees/{sha}?recursive=1"
    status, text, error = fetch_cached(url, token, cache_dir, repo, sha, "__tree__.json")
    if status != "ok":
        return [{"path": "", "role": "", "discovered_by": "tree_error", "raw_url": "", "error": error or status}]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [{"path": "", "role": "", "discovered_by": "tree_error", "raw_url": "", "error": "invalid_json"}]
    language = candidate.get("repo_language", "")
    files = select_tree_manifest_files(
        data.get("tree", []) or [], language, max_files, import_packages,
    )
    for item in files:
        item["raw_url"] = raw_url(repo, sha, item["path"])
    return files


NPM_WORKSPACE_DIRS = {"packages", "apps", "libs", "modules", "workspaces"}


def npm_package_basename(package: str) -> str:
    return package.strip().rstrip("/").rsplit("/", 1)[-1].lower()


def tree_manifest_priority(
    path: str, language: str, import_basenames: set[str] | None = None,
) -> tuple[int, int, str]:
    parts = Path(path).parts
    if language in {"JavaScript", "TypeScript"} and Path(path).name.lower() == "package.json":
        if len(parts) == 1:
            return 0, len(parts), path.lower()
        if parts[-2].lower() in (import_basenames or set()):
            return 1, len(parts), path.lower()
        if parts[0].lower() in NPM_WORKSPACE_DIRS:
            return 2, len(parts), path.lower()
        return 3, len(parts), path.lower()
    return 4, len(parts), path.lower()


def select_tree_manifest_files(
    tree: list[dict], language: str, max_files: int,
    import_packages: set[str] | None = None,
) -> list[dict[str, str]]:
    files = []
    for item in tree:
        path = str(item.get("path", ""))
        if item.get("type") == "blob" and is_candidate_file(path, language):
            files.append({
                "path": path, "role": file_role(path, language),
                "discovered_by": "tree", "raw_url": "",
            })
    basenames = {
        npm_package_basename(package) for package in (import_packages or set())
        if package
    }
    files.sort(
        key=lambda item: tree_manifest_priority(item["path"], language, basenames)
    )
    return files[:max(1, max_files)]


def parse_dependency_file(language: str, path: str, text: str) -> list[dict[str, str]]:
    base = Path(path).name.lower()
    if language in {"JavaScript", "TypeScript"}:
        if base == "package.json":
            return parse_package_json(path, text)
        if base == "package-lock.json":
            return parse_package_lock(path, text)
        if base == "yarn.lock":
            return parse_yarn_lock(path, text)
        if base == "pnpm-lock.yaml":
            return parse_pnpm_lock(path, text)
    if language == "Python":
        if base.startswith("requirements") and base.endswith(".txt"):
            return parse_requirements(path, text)
        if base == "poetry.lock":
            return parse_poetry_lock(path, text)
        if base == "pipfile.lock":
            return parse_pipfile_lock(path, text)
        if base == "uv.lock":
            return parse_uv_lock(path, text)
        if base == "pyproject.toml":
            return parse_pyproject_basic(path, text)
    if language == "Rust":
        if base == "cargo.toml":
            return parse_cargo_toml(path, text)
        if base == "cargo.lock":
            return parse_cargo_lock(path, text)
    if language == "Go":
        if base == "go.mod":
            return parse_go_mod(path, text)
        if base == "go.sum":
            return parse_go_sum(path, text)
    if language in {"Java", "Kotlin", "Scala", "Groovy", "Clojure"}:
        if base == "pom.xml":
            return parse_maven_pom(path, text)
        if base in {"build.gradle", "build.gradle.kts"}:
            return parse_gradle_dependencies(path, text, base)
        if base == "libs.versions.toml":
            return parse_gradle_version_catalog(path, text)
        if base == "gradle.lockfile":
            return parse_gradle_lockfile(path, text)
    if language == "PHP":
        if base == "composer.json":
            return parse_composer_json(path, text)
        if base == "composer.lock":
            return parse_composer_lock(path, text)
    if language in {"C#", "F#"}:
        if base.endswith((".csproj", ".fsproj", ".vbproj")) or base in {"packages.config", "directory.packages.props"}:
            return parse_nuget_xml(path, text)
        if base == "packages.lock.json":
            return parse_nuget_lock(path, text)
    if language in {"C", "C++", "C_C++_Header", "Objective-C"}:
        if base == "vcpkg.json":
            return parse_vcpkg_json(path, text)
        if base.startswith("conanfile"):
            return parse_conanfile(path, text)
    return []


def dep_row(path: str, name: str, spec: str, source: str, kind: str, group: str, namespace_hint: str = "") -> dict[str, str]:
    return {
        "dep_file_path": path,
        "package_name": name,
        "version_spec": spec,
        "resolved_version": clean_exact_version(spec) if kind in {"exact", "resolved"} else "",
        "resolution_source": source,
        "version_kind": kind,
        "dependency_group": group,
        "namespace_hint": namespace_hint,
    }


def clean_exact_version(spec: str) -> str:
    value = (spec or "").strip().strip('"').strip("'")
    for prefix in ("===", "==", "="):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    return value


def version_kind(spec: str) -> str:
    spec = (spec or "").strip().strip('"')
    if not spec:
        return "unresolved"
    if spec.startswith(("file:", "workspace:", "path", "git", "http")) or "git" in spec:
        return "non_registry"
    cleaned = spec.lstrip("=").strip()
    if re.fullmatch(r"v?\d+(?:\.\d+)*(?:[-+][A-Za-z0-9_.-]+)?", cleaned):
        return "exact"
    return "range"


def parse_npm_alias_spec(spec: str) -> tuple[str, str]:
    value = (spec or "").strip()
    if not value.startswith("npm:"):
        return "", ""
    body = value[4:]
    separator = body.rfind("@")
    if separator <= 0:
        return body, ""
    return body[:separator], body[separator + 1:]


def parse_package_json(path: str, text: str) -> list[dict[str, str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = []
    custom_registry = npm_custom_registry(data)
    package_name = str(data.get("name") or "").strip()
    if package_name:
        rows.append(dep_row(
            path, package_name, str(data.get("version") or ""),
            "first_party_module", "non_registry", "npm_package", package_name,
        ))
    for section in ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"]:
        deps = data.get(section) or {}
        if isinstance(deps, dict):
            for name, spec in deps.items():
                group = npm_dependency_group(section, str(name), custom_registry)
                alias_target, alias_spec = parse_npm_alias_spec(str(spec))
                if alias_target:
                    rows.append(dep_row(
                        path, str(name), alias_spec, "manifest_npm_alias",
                        version_kind(alias_spec), f"{group};npm.alias", alias_target,
                    ))
                else:
                    rows.append(dep_row(path, str(name), str(spec), "manifest", version_kind(str(spec)), group))
    return rows


def npm_custom_registry(data: dict) -> str:
    # publishConfig configures publication of this package, not installation of
    # its dependencies. Retain an explicit legacy root hint conservatively.
    registry = str(data.get("registry") or "")
    registry = registry.strip()
    if registry and "registry.npmjs.org" not in registry and "npmjs.com" not in registry:
        return registry
    return ""


def npm_dependency_group(section: str, name: str, custom_registry: str) -> str:
    if custom_registry and name.startswith("@"):
        return f"{section};npm.custom_registry:{custom_registry}"
    return section


def parse_package_lock(path: str, text: str) -> list[dict[str, str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = []
    packages = data.get("packages") or {}
    if isinstance(packages, dict):
        for key, value in packages.items():
            if key.startswith("node_modules/") and isinstance(value, dict) and value.get("version"):
                rows.append(dep_row(path, key.removeprefix("node_modules/"), str(value["version"]), "lockfile_possible_transitive", "resolved", "lockfile"))
    deps = data.get("dependencies") or {}
    if isinstance(deps, dict):
        for name, value in deps.items():
            if isinstance(value, dict) and value.get("version"):
                rows.append(dep_row(path, str(name), str(value["version"]), "lockfile_possible_transitive", "resolved", "lockfile"))
    return rows




def parse_yarn_lock(path: str, text: str) -> list[dict[str, str]]:
    rows = []
    current_names: list[str] = []
    current_version = ""
    for raw in text.splitlines() + [""]:
        if raw and not raw.startswith((" ", "\t")) and raw.rstrip().endswith(":"):
            if current_version:
                for name in current_names:
                    rows.append(dep_row(path, name, current_version, "lockfile_possible_transitive", "resolved", "yarn.lock"))
            header = raw.rstrip()[:-1].strip().strip('\"')
            current_names = [normalize_npm_lock_name(part.strip()) for part in split_yarn_header(header)]
            current_names = [name for name in current_names if name]
            current_version = ""
            continue
        match = re.match(r"\s+version\s+[\'\"]([^\'\"]+)[\'\"]", raw)
        if match:
            current_version = match.group(1)
    return rows


def split_yarn_header(header: str) -> list[str]:
    return [part.strip().strip('\"').strip("'") for part in header.split(",")]


def normalize_npm_lock_name(value: str) -> str:
    if not value:
        return ""
    value = value.strip().strip('\"').strip("'")
    if value.startswith("@"):
        parts = value.split("@")
        return "@" + parts[1] if len(parts) > 1 else value
    return value.split("@", 1)[0]


def parse_pnpm_lock(path: str, text: str) -> list[dict[str, str]]:
    rows = []
    for raw in text.splitlines():
        match = re.match(r"\s{2,}(/(?:@[^/]+/)?[^/@][^:]*?)@([^(:]+)", raw)
        if not match:
            continue
        name = match.group(1).lstrip("/")
        version = match.group(2).strip()
        if name and version:
            rows.append(dep_row(path, name, version, "lockfile_possible_transitive", "resolved", "pnpm-lock.yaml"))
    return rows


def normalize_pypi(name: str) -> str:
    return name.replace("_", "-").lower()


def parse_requirements(path: str, text: str) -> list[dict[str, str]]:
    rows = []
    for raw in text.splitlines():
        line = raw.strip().split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)\s*([<>=!~]=?[^;\s]+)?", line)
        if match:
            spec = (match.group(2) or "").strip()
            rows.append(dep_row(path, normalize_pypi(match.group(1)), spec, "manifest", version_kind(spec), "requirements"))
    return rows


def parse_poetry_lock(path: str, text: str) -> list[dict[str, str]]:
    rows = []
    current = {}
    for line in text.splitlines() + ["[[package]]"]:
        if line.strip() == "[[package]]":
            if current.get("name") and current.get("version"):
                rows.append(dep_row(path, normalize_pypi(current["name"]), current["version"], "lockfile_possible_transitive", "resolved", "lockfile"))
            current = {}
            continue
        match = re.match(r"(name|version)\s*=\s*['\"]([^'\"]+)['\"]", line.strip())
        if match:
            current[match.group(1)] = match.group(2)
    return rows




def parse_pipfile_lock(path: str, text: str) -> list[dict[str, str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = []
    for section in ["default", "develop"]:
        deps = data.get(section) or {}
        if isinstance(deps, dict):
            for name, item in deps.items():
                version = item.get("version", "") if isinstance(item, dict) else ""
                rows.append(dep_row(path, normalize_pypi(str(name)), str(version), "lockfile_possible_transitive", version_kind(str(version)), section))
    return rows


def parse_uv_lock(path: str, text: str) -> list[dict[str, str]]:
    if tomllib is None:
        return []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    rows = []
    packages = data.get("package") if isinstance(data, dict) else []
    if isinstance(packages, list):
        for item in packages:
            if isinstance(item, dict) and item.get("name") and item.get("version"):
                rows.append(dep_row(path, normalize_pypi(str(item["name"])), str(item["version"]), "lockfile_possible_transitive", "resolved", "uv.lock"))
    return rows


def parse_pyproject_basic(path: str, text: str) -> list[dict[str, str]]:
    if tomllib is None:
        return []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    rows = []
    project = data.get("project") if isinstance(data, dict) else {}
    if isinstance(project, dict):
        rows.extend(parse_dependency_strings(path, project.get("dependencies"), "project.dependencies"))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group, deps in optional.items():
                rows.extend(parse_dependency_strings(path, deps, f"project.optional.{group}"))
    tool = data.get("tool") if isinstance(data, dict) else {}
    poetry = tool.get("poetry") if isinstance(tool, dict) else {}
    if isinstance(poetry, dict):
        rows.extend(parse_named_dependency_table(path, poetry.get("dependencies"), "poetry.dependencies"))
        rows.extend(parse_named_dependency_table(path, poetry.get("dev-dependencies"), "poetry.dev-dependencies"))
        groups = poetry.get("group")
        if isinstance(groups, dict):
            for group, value in groups.items():
                if isinstance(value, dict):
                    rows.extend(parse_named_dependency_table(path, value.get("dependencies"), f"poetry.group.{group}"))
    pdm = tool.get("pdm") if isinstance(tool, dict) else {}
    if isinstance(pdm, dict):
        dev_deps = pdm.get("dev-dependencies")
        if isinstance(dev_deps, dict):
            for group, deps in dev_deps.items():
                rows.extend(parse_dependency_strings(path, deps, f"pdm.dev.{group}"))
    return rows


def parse_dependency_strings(path: str, deps: object, group: str) -> list[dict[str, str]]:
    rows = []
    if not isinstance(deps, list):
        return rows
    for dep in deps:
        if isinstance(dep, str):
            rows.extend(row_from_python_requirement(path, dep, group))
    return rows


def parse_named_dependency_table(path: str, deps: object, group: str) -> list[dict[str, str]]:
    rows = []
    if not isinstance(deps, dict):
        return rows
    for name, spec in deps.items():
        if str(name).lower() == "python":
            continue
        if isinstance(spec, dict):
            spec_value = spec.get("version") or spec.get("path") or spec.get("git") or ""
        else:
            spec_value = spec
        rows.append(dep_row(path, normalize_pypi(str(name)), str(spec_value), "manifest", version_kind(str(spec_value)), group))
    return rows


def row_from_python_requirement(path: str, requirement: str, group: str) -> list[dict[str, str]]:
    line = requirement.strip().split(";", 1)[0].strip()
    match = re.match(r"([A-Za-z0-9_.-]+)\s*([<>=!~]=?.*)?", line)
    if not match:
        return []
    spec = (match.group(2) or "").strip()
    return [dep_row(path, normalize_pypi(match.group(1)), spec, "manifest", version_kind(spec), group)]


def parse_cargo_toml(path: str, text: str) -> list[dict[str, str]]:
    rows = []
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r"\[(.+)]", stripped)
        if match:
            section = match.group(1)
            continue
        if section not in {"dependencies", "dev-dependencies", "build-dependencies"}:
            continue
        dep = re.match(r"([A-Za-z0-9_-]+)\s*=\s*(.+)", stripped)
        if dep:
            raw_spec = dep.group(2).strip().strip('"')
            version_match = re.search("version\\s*=\\s*[\'\"]([^\'\"]+)[\'\"]", raw_spec)
            spec_value = version_match.group(1) if version_match else raw_spec
            rows.append(dep_row(path, dep.group(1).replace("_", "-"), spec_value, "manifest", cargo_manifest_version_kind(raw_spec), section))
    return rows


def cargo_manifest_version_kind(spec: str) -> str:
    value = (spec or "").strip().strip('"').strip("'").lower()
    if not value:
        return "unresolved"
    if "workspace" in value or "path" in value or "git" in value:
        return "non_registry"
    if value.startswith("="):
        return "exact"
    return "range"


def parse_cargo_lock(path: str, text: str) -> list[dict[str, str]]:
    rows = []
    current = {}
    for line in text.splitlines() + ["[[package]]"]:
        if line.strip() == "[[package]]":
            if current.get("name") and current.get("version"):
                source = current.get("source", "")
                is_registry = source.startswith("registry+") or "crates.io-index" in source
                rows.append(dep_row(
                    path,
                    current["name"].replace("_", "-"),
                    current["version"],
                    "lockfile_possible_transitive" if is_registry else "lockfile_local_workspace",
                    "resolved" if is_registry else "non_registry",
                    "lockfile",
                ))
            current = {}
            continue
        match = re.match("(name|version|source)\\s*=\\s*[\'\"]([^\'\"]+)[\'\"]", line.strip())
        if match:
            current[match.group(1)] = match.group(2)
    return rows


def parse_go_mod(path: str, text: str) -> list[dict[str, str]]:
    rows = []
    local_replacements = go_local_replacements(text)
    in_require = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line == "require (":
            in_require = True
            continue
        if in_require and line == ")":
            in_require = False
            continue
        if line.startswith("require "):
            line = line.removeprefix("require ").strip()
        elif not in_require:
            continue
        if "=>" in line:
            continue
        parts = line.split()
        if len(parts) >= 2 and "/" in parts[0]:
            module, version = parts[0], parts[1]
            if module in local_replacements:
                rows.append(dep_row(path, module, version, "manifest", "non_registry", f"require;go.local_replace:{local_replacements[module]}"))
            else:
                rows.append(dep_row(path, module, version, "manifest", "exact", "require"))
    return rows


def go_local_replacements(text: str) -> dict[str, str]:
    replacements: dict[str, str] = {}
    in_replace = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line == "replace (":
            in_replace = True
            continue
        if in_replace and line == ")":
            in_replace = False
            continue
        if line.startswith("replace "):
            line = line.removeprefix("replace ").strip()
        elif not in_replace:
            continue
        if "=>" not in line:
            continue
        left, right = [part.strip() for part in line.split("=>", 1)]
        module = left.split()[0] if left.split() else ""
        target = right.split()[0] if right.split() else ""
        if module and is_local_go_replace_target(target):
            replacements[module] = target
    return replacements


def is_local_go_replace_target(target: str) -> bool:
    value = (target or "").strip()
    return value.startswith(("./", "../", "/")) or value in {".", ".."}



def parse_go_sum(path: str, text: str) -> list[dict[str, str]]:
    rows = []
    seen = set()
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) < 2 or not parts[0] or not parts[1].startswith("v"):
            continue
        version = parts[1].removesuffix("/go.mod")
        key = (parts[0], version)
        if key in seen:
            continue
        seen.add(key)
        rows.append(dep_row(path, parts[0], version, "lockfile_possible_transitive", "resolved", "go.sum"))
    return rows


def parse_go_module_path(text: str) -> str:
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if line.startswith("module "):
            return line.removeprefix("module ").strip()
    return ""



MAVEN_MANAGED_FIELDS = [
    "repo",
    "sha",
    "dep_file_path",
    "package_name",
    "version_before",
    "resolved_version",
    "managed_source",
    "managed_source_path",
    "evidence",
]


JVM_LANGUAGES = {"Java", "Kotlin", "Scala", "Groovy", "Clojure"}


def parse_maven_pom(path: str, text: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    properties = collect_maven_properties(root)
    rows = []
    deps_node = xml_direct_child(root, 'dependencies')
    for dep in maven_dependency_children(deps_node):
        group = resolve_maven_property(xml_child_text(dep, 'groupId'), properties)
        artifact = resolve_maven_property(xml_child_text(dep, 'artifactId'), properties)
        version = resolve_maven_property(xml_child_text(dep, 'version'), properties)
        scope = xml_child_text(dep, 'scope') or 'dependency'
        if group and artifact:
            rows.append(dep_row(path, f'{group}:{artifact}', version, 'manifest', maven_version_kind(version), scope))
    return rows


def maven_dependency_children(node: ET.Element | None) -> list[ET.Element]:
    if node is None:
        return []
    return [child for child in list(node) if child.tag.split('}', 1)[-1] == 'dependency']


def enrich_maven_managed_versions(
    language: str,
    dep_rows: list[dict[str, str]],
    fetched_texts: list[dict[str, str]],
    cache_dir: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if language not in JVM_LANGUAGES:
        return dep_rows, []
    local_models = {}
    for item in fetched_texts:
        path = item.get('path', '')
        if Path(path).name.lower() != 'pom.xml':
            continue
        model = parse_maven_model(path, item.get('text', ''))
        if model:
            local_models[path.replace('\\', '/')] = model
    if not local_models:
        return dep_rows, []
    memo: dict[str, dict[str, tuple[str, str]]] = {}
    out = []
    audit_rows = []
    for row in dep_rows:
        if not should_resolve_maven_managed(row):
            out.append(row)
            continue
        model = local_models.get(row.get('dep_file_path', '').replace('\\', '/'))
        package = row.get('package_name', '')
        if not model:
            out.append(row)
            audit_rows.append(maven_managed_audit_row(row, '', '', '', 'pom_model_not_found'))
            continue
        managed = effective_maven_management(model, local_models, cache_dir, memo, set())
        version, source = managed.get(package, ('', ''))
        if version and maven_version_kind(version) == 'exact':
            updated = dict(row)
            updated['version_spec'] = version
            updated['version_kind'] = 'exact'
            updated['dependency_group'] = semicolon_join(updated.get('dependency_group', ''), 'maven.dependency_management')
            out.append(updated)
            audit_rows.append(maven_managed_audit_row(row, version, source, 'resolved', 'dependency_management'))
        else:
            out.append(row)
            audit_rows.append(maven_managed_audit_row(row, version, source, 'not_resolved', 'package_not_listed_or_non_exact'))
    return out, audit_rows


def should_resolve_maven_managed(row: dict[str, str]) -> bool:
    return (
        row.get('repo_language') in JVM_LANGUAGES
        and Path(row.get('dep_file_path', '')).name.lower() == 'pom.xml'
        and row.get('resolution_source') == 'manifest'
        and row.get('version_kind') == 'unresolved'
        and bool(row.get('package_name'))
    )


def parse_maven_model(path: str, text: str) -> dict[str, object] | None:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    props = collect_maven_properties(root)
    parent = xml_direct_child(root, 'parent')
    parent_coords = {
        'group': resolve_maven_property(xml_child_text(parent, 'groupId'), props),
        'artifact': resolve_maven_property(xml_child_text(parent, 'artifactId'), props),
        'version': resolve_maven_property(xml_child_text(parent, 'version'), props),
        'relative_path': xml_child_text(parent, 'relativePath') or '../pom.xml',
    } if parent is not None else {}
    project_group = props.get('project.groupId', '')
    project_artifact = props.get('project.artifactId', '')
    project_version = props.get('project.version', '')
    managed, imports = parse_maven_dependency_management(root, props)
    return {
        'path': path.replace('\\', '/'),
        'coords': (project_group, project_artifact, project_version),
        'parent': parent_coords,
        'managed': managed,
        'imports': imports,
    }


def parse_maven_dependency_management(root: ET.Element, properties: dict[str, str]) -> tuple[dict[str, tuple[str, str]], list[tuple[str, str, str]]]:
    managed: dict[str, tuple[str, str]] = {}
    imports: list[tuple[str, str, str]] = []
    dm = xml_direct_child(root, 'dependencyManagement')
    deps_node = xml_direct_child(dm, 'dependencies') if dm is not None else None
    for dep in maven_dependency_children(deps_node):
        group = resolve_maven_property(xml_child_text(dep, 'groupId'), properties)
        artifact = resolve_maven_property(xml_child_text(dep, 'artifactId'), properties)
        version = resolve_maven_property(xml_child_text(dep, 'version'), properties)
        dep_type = resolve_maven_property(xml_child_text(dep, 'type'), properties)
        scope = resolve_maven_property(xml_child_text(dep, 'scope'), properties)
        if not group or not artifact or not version:
            continue
        package = f'{group}:{artifact}'
        if dep_type == 'pom' and scope == 'import' and maven_version_kind(version) == 'exact':
            imports.append((group, artifact, version))
        elif maven_version_kind(version) == 'exact':
            managed[package] = (version, 'same_pom_dependency_management')
    return managed, imports


def effective_maven_management(
    model: dict[str, object],
    local_models: dict[str, dict[str, object]],
    cache_dir: Path,
    memo: dict[str, dict[str, tuple[str, str]]],
    seen: set[str],
) -> dict[str, tuple[str, str]]:
    key = maven_model_key(model)
    if key in memo:
        return memo[key]
    if key in seen:
        return {}
    seen.add(key)
    managed: dict[str, tuple[str, str]] = {}
    parent_model = resolve_maven_parent_model(model, local_models, cache_dir)
    if parent_model:
        managed.update(effective_maven_management(parent_model, local_models, cache_dir, memo, seen))
    for group, artifact, version in model.get('imports', []):
        imported = fetch_maven_model(group, artifact, version, cache_dir)
        if imported:
            imported_management = effective_maven_management(imported, local_models, cache_dir, memo, seen)
            for package, (resolved, source) in imported_management.items():
                managed[package] = (resolved, source or f'external_import:{group}:{artifact}:{version}')
    for package, (version, source) in model.get('managed', {}).items():
        managed[package] = (version, f'{source}:{model.get("path", "")}')
    memo[key] = managed
    seen.discard(key)
    return managed


def maven_model_key(model: dict[str, object]) -> str:
    coords = model.get('coords', ('', '', ''))
    if isinstance(coords, tuple) and any(coords):
        return ':'.join(str(part) for part in coords)
    return str(model.get('path', ''))


def resolve_maven_parent_model(model: dict[str, object], local_models: dict[str, dict[str, object]], cache_dir: Path) -> dict[str, object] | None:
    parent = model.get('parent')
    if not isinstance(parent, dict) or not parent.get('group') or not parent.get('artifact'):
        return None
    local = local_parent_model(model, parent, local_models)
    if local:
        return local
    version = str(parent.get('version', ''))
    if maven_version_kind(version) != 'exact':
        return None
    return fetch_maven_model(str(parent.get('group')), str(parent.get('artifact')), version, cache_dir)


def local_parent_model(model: dict[str, object], parent: dict[str, object], local_models: dict[str, dict[str, object]]) -> dict[str, object] | None:
    path = str(model.get('path', '')).replace('\\', '/')
    relative = str(parent.get('relative_path') or '../pom.xml')
    parent_coords = (str(parent.get('group', '')), str(parent.get('artifact', '')), str(parent.get('version', '')))
    if relative:
        candidate = normalize_posix_path(str(Path(path).parent / relative))
        model = local_models.get(candidate)
        if model and model.get('coords') == parent_coords:
            return model
    for item in local_models.values():
        if item.get('coords') == parent_coords:
            return item
    return None


def normalize_posix_path(value: str) -> str:
    parts = []
    for part in value.replace('\\', '/').split('/'):
        if not part or part == '.':
            continue
        if part == '..':
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return '/'.join(parts)


def fetch_maven_model(group: str, artifact: str, version: str, cache_dir: Path) -> dict[str, object] | None:
    if not group or not artifact or maven_version_kind(version) != 'exact':
        return None
    text = fetch_maven_pom_text(group, artifact, version, cache_dir)
    if not text:
        return None
    return parse_maven_model(f'external:{group}:{artifact}:{version}', text)


def fetch_maven_pom_text(group: str, artifact: str, version: str, cache_dir: Path) -> str:
    target = cache_dir / 'maven_poms' / f'{safe_name(group)}__{safe_name(artifact)}__{safe_name(version)}.pom'
    cached, _actual_path = read_cache_text(target)
    if cached is not None:
        return cached
    group_path = '/'.join(group.split('.'))
    urls = [
        f'https://repo1.maven.org/maven2/{group_path}/{artifact}/{version}/{artifact}-{version}.pom',
        f'https://dl.google.com/dl/android/maven2/{group_path}/{artifact}/{version}/{artifact}-{version}.pom',
    ]
    for url in urls:
        status, text, _error = request_text(url, '')
        if status == 200 and text:
            write_gzip_text(target, text)
            return text
    return ''


def maven_managed_audit_row(row: dict[str, str], resolved: str, source: str, status: str, evidence: str) -> dict[str, str]:
    return {
        'repo': row.get('repo', ''),
        'sha': row.get('sha', ''),
        'dep_file_path': row.get('dep_file_path', ''),
        'package_name': row.get('package_name', ''),
        'version_before': row.get('version_spec', ''),
        'resolved_version': resolved,
        'managed_source': status,
        'managed_source_path': source,
        'evidence': evidence,
    }


def semicolon_join(existing: str, note: str) -> str:
    parts = [part for part in str(existing or '').split(';') if part]
    if note not in parts:
        parts.append(note)
    return ';'.join(parts)


def collect_maven_properties(root: ET.Element) -> dict[str, str]:
    properties: dict[str, str] = {}
    parent = xml_direct_child(root, 'parent')
    project_group = xml_child_text(root, 'groupId') or (xml_child_text(parent, 'groupId') if parent is not None else '')
    project_artifact = xml_child_text(root, 'artifactId')
    project_version = xml_child_text(root, 'version') or (xml_child_text(parent, 'version') if parent is not None else '')
    for key, value in {
        'project.groupId': project_group,
        'pom.groupId': project_group,
        'project.artifactId': project_artifact,
        'pom.artifactId': project_artifact,
        'project.version': project_version,
        'pom.version': project_version,
        'version': project_version,
    }.items():
        if value:
            properties[key] = value
    props_node = xml_direct_child(root, 'properties')
    if props_node is not None:
        for child in list(props_node):
            key = child.tag.split('}', 1)[-1]
            value = (child.text or '').strip()
            if key and value:
                properties[key] = value
    return properties


def resolve_maven_property(value: str, properties: dict[str, str]) -> str:
    resolved = (value or '').strip()
    if not resolved:
        return ''
    for _ in range(5):
        changed = False

        def replace(match: re.Match[str]) -> str:
            nonlocal changed
            key = match.group(1)
            if key in properties:
                changed = True
                return properties[key]
            return match.group(0)

        updated = re.sub(r'\$\{([^}]+)\}', replace, resolved)
        resolved = updated.strip()
        if not changed:
            break
    return resolved


def xml_direct_child(node: ET.Element, local_name: str) -> ET.Element | None:
    for child in list(node):
        if child.tag.split('}', 1)[-1] == local_name:
            return child
    return None


def xml_child_text(node: ET.Element | None, local_name: str) -> str:
    if node is None:
        return ''
    for child in list(node):
        if child.tag.split('}', 1)[-1] == local_name and child.text:
            return child.text.strip()
    return ''


def maven_version_kind(version: str) -> str:
    value = (version or '').strip()
    if not value:
        return 'unresolved'
    if value.startswith('${') or value.startswith('[') or value.startswith('('):
        return 'range'
    return version_kind(value)


def parse_gradle_dependencies(path: str, text: str, base: str) -> list[dict[str, str]]:
    rows = []
    variables = gradle_literal_version_variables(text)
    coordinate_pattern = re.compile(r"['\"]([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([^'\"]+)['\"]")
    map_patterns = [
        re.compile(r"group\s*[:=]\s*['\"]([^'\"]+)['\"].*?name\s*[:=]\s*['\"]([^'\"]+)['\"].*?version\s*[:=]\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"group\s*[:=]\s*['\"]([^'\"]+)['\"].*?name\s*[:=]\s*['\"]([^'\"]+)['\"].*?version\s*[:=]\s*([A-Za-z_][A-Za-z0-9_.-]*)"),
    ]
    for raw in text.splitlines():
        line = raw.split('//', 1)[0].strip()
        if not line or line.startswith(('#', '*')):
            continue
        matched = False
        for match in coordinate_pattern.finditer(line):
            group, artifact, version = match.groups()
            version = version.strip()
            resolved = resolve_gradle_version_token(version, variables)
            group_label = gradle_dependency_group(base, version, variables)
            rows.append(dep_row(path, f'{group}:{artifact}', resolved, 'manifest', maven_version_kind(resolved), group_label))
            matched = True
        if matched:
            continue
        for pattern in map_patterns:
            match = pattern.search(line)
            if match:
                group, artifact, version = match.groups()
                version = version.strip()
                resolved = resolve_gradle_version_token(version, variables)
                group_label = gradle_dependency_group(base, version, variables)
                rows.append(dep_row(path, f'{group}:{artifact}', resolved, 'manifest', maven_version_kind(resolved), group_label))
                break
    return rows


def gradle_literal_version_variables(text: str) -> dict[str, str]:
    variables: dict[str, str] = {}
    patterns = [
        re.compile(r"(?:^|\s)(?:def\s+|val\s+|var\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"ext\.([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"extra\[['\"]([^'\"]+)['\"]\]\s*=\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"set\(['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\)"),
        re.compile(r"([A-Za-z_][A-Za-z0-9_.-]*)\s+by\s+extra\(['\"]([^'\"]+)['\"]\)"),
    ]
    for raw in text.splitlines():
        line = raw.split('//', 1)[0].strip()
        if not line or line.startswith(('#', '*')):
            continue
        for pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            name, value = match.groups()
            value = value.strip()
            if exact_gradle_literal_version(value):
                variables[name] = value
    return variables


def resolve_gradle_version_token(version: str, variables: dict[str, str]) -> str:
    value = (version or '').strip()
    name = gradle_version_variable_name(value, variables)
    if name:
        return variables[name]
    return value


def gradle_dependency_group(base: str, version: str, variables: dict[str, str]) -> str:
    name = gradle_version_variable_name(version, variables)
    return f"{base};gradle.version_variable:{name}" if name else base


def gradle_version_variable_name(version: str, variables: dict[str, str]) -> str:
    value = (version or '').strip()
    match = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_.-]*)\}?", value)
    if match and match.group(1) in variables:
        return match.group(1)
    if value in variables:
        return value
    return ''


def exact_gradle_literal_version(value: str) -> bool:
    return bool(re.fullmatch(r"v?\d+(?:\.\d+)*(?:[-+][A-Za-z0-9_.-]+)?", (value or '').strip()))



def parse_gradle_version_catalog(path: str, text: str) -> list[dict[str, str]]:
    data = parse_toml_document(text)
    versions = data.get("versions", {}) if isinstance(data, dict) else {}
    libraries = data.get("libraries", {}) if isinstance(data, dict) else {}
    if not isinstance(libraries, dict):
        return []
    rows = []
    for alias, spec in libraries.items():
        package, version = gradle_catalog_library(spec, versions)
        if package:
            rows.append(dep_row(path, package, version, "manifest", maven_version_kind(version), f"gradle.version_catalog:{alias}"))
    return rows


def parse_toml_document(text: str) -> dict:
    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except Exception:
            return parse_simple_gradle_catalog(text)
    return parse_simple_gradle_catalog(text)


def parse_simple_gradle_catalog(text: str) -> dict:
    data: dict[str, dict[str, object]] = {"versions": {}, "libraries": {}}
    section = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line.strip("[]").strip()
            continue
        if "=" not in line or section not in data:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        data[section][key] = parse_simple_toml_value(value)
    return data


def parse_simple_toml_value(value: str) -> object:
    value = value.strip().rstrip(",")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    if value.startswith("{") and value.endswith("}"):
        item: dict[str, object] = {}
        body = value[1:-1]
        for part in split_simple_inline_table(body):
            if "=" not in part:
                continue
            key, val = [chunk.strip() for chunk in part.split("=", 1)]
            parsed = parse_simple_toml_value(val)
            if "." in key:
                left, right = key.split(".", 1)
                nested = item.setdefault(left, {})
                if isinstance(nested, dict):
                    nested[right] = parsed
            else:
                item[key] = parsed
        return item
    return value


def split_simple_inline_table(body: str) -> list[str]:
    parts = []
    current = []
    quote = ""
    for char in body:
        if char in {'"', "'"}:
            quote = "" if quote == char else char if not quote else quote
        if char == "," and not quote:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return parts

def gradle_catalog_library(spec: object, versions: object) -> tuple[str, str]:
    if isinstance(spec, str):
        parts = spec.split(":")
        if len(parts) >= 3:
            return f"{parts[0]}:{parts[1]}", parts[2]
        if len(parts) == 2:
            return spec, ""
        return "", ""
    if not isinstance(spec, dict):
        return "", ""
    module = str(spec.get("module") or "")
    if not module and spec.get("group") and spec.get("name"):
        module = f"{spec.get('group')}:{spec.get('name')}"
    version = gradle_catalog_version(spec.get("version"), versions)
    return module, version


def gradle_catalog_version(value: object, versions: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        ref = str(value.get("ref") or "")
        if ref and isinstance(versions, dict):
            resolved = versions.get(ref, "")
            if isinstance(resolved, str):
                return resolved
            if isinstance(resolved, dict):
                return str(resolved.get("strictly") or resolved.get("require") or resolved.get("prefer") or "")
        return str(value.get("strictly") or value.get("require") or value.get("prefer") or "")
    return ""


def build_gradle_catalog_usage_rows(language: str, fetched_files: list[dict[str, str]]) -> list[dict[str, str]]:
    if language not in {"Java", "Kotlin", "Scala", "Groovy", "Clojure"}:
        return []
    catalogs = []
    for item in fetched_files:
        path = item.get("path", "")
        if not path.replace("\\", "/").endswith("gradle/libs.versions.toml"):
            continue
        alias_map = gradle_catalog_alias_map(item.get("text", ""))
        if alias_map:
            catalogs.append({"path": path, "scope": gradle_catalog_scope(path), "aliases": alias_map})
    if not catalogs:
        return []
    rows = []
    seen = set()
    for item in fetched_files:
        build_path = item.get("path", "")
        base = Path(build_path).name.lower()
        if base not in {"build.gradle", "build.gradle.kts"}:
            continue
        build_text = item.get("text", "")
        used_aliases = extract_gradle_lib_aliases(build_text)
        if not used_aliases:
            continue
        platform_aliases = extract_gradle_platform_aliases(build_text)
        direct_platforms = extract_gradle_direct_platforms(build_text)
        for catalog in catalogs:
            if not gradle_same_scope(build_path, catalog["scope"]):
                continue
            aliases = catalog["aliases"]
            platform_deps = gradle_platform_dependencies(platform_aliases, direct_platforms, aliases)
            for used in used_aliases:
                if used not in aliases:
                    continue
                package, version, original_alias = aliases[used]
                kind = maven_version_kind(version)
                group = f"gradle.version_catalog_used:{original_alias}"
                if not version:
                    bom_note = gradle_bom_management_note(package, platform_deps)
                    if bom_note:
                        kind = "bom_managed"
                        group = f"{group};{bom_note}"
                key = (build_path, package, version, original_alias, kind, group)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(dep_row(build_path, package, version, "manifest", kind, group))
    return rows


def gradle_catalog_alias_map(text: str) -> dict[str, tuple[str, str, str]]:
    data = parse_toml_document(text)
    versions = data.get("versions", {}) if isinstance(data, dict) else {}
    libraries = data.get("libraries", {}) if isinstance(data, dict) else {}
    if not isinstance(libraries, dict):
        return {}
    output: dict[str, tuple[str, str, str]] = {}
    for alias, spec in libraries.items():
        package, version = gradle_catalog_library(spec, versions)
        if package:
            output[normalize_gradle_alias(str(alias))] = (package, version, str(alias))
    return output


def extract_gradle_platform_aliases(text: str) -> set[str]:
    aliases = set()
    pattern = r"\b(?:enforcedPlatform|platform)\s*\(\s*libs((?:\.[A-Za-z_][A-Za-z0-9_-]*)+)"
    for match in re.finditer(pattern, text):
        value = match.group(1).strip(".")
        if value:
            aliases.add(normalize_gradle_alias(value))
    return aliases


def extract_gradle_direct_platforms(text: str) -> list[tuple[str, str, str]]:
    platforms = []
    pattern = r'\b(?:enforcedPlatform|platform)\s*\(\s*["\']([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+):([^"\']+)["\']'
    for group, artifact, version in re.findall(pattern, text):
        platforms.append((f"{group}:{artifact}", version.strip(), f"{group}:{artifact}"))
    return platforms


def gradle_platform_dependencies(
    platform_aliases: set[str],
    direct_platforms: list[tuple[str, str, str]],
    aliases: dict[str, tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    platforms = list(direct_platforms)
    for alias in platform_aliases:
        if alias not in aliases:
            continue
        package, version, original_alias = aliases[alias]
        platforms.append((package, version, original_alias))
    return [item for item in platforms if is_maven_bom_package(item[0])]


def is_maven_bom_package(package: str) -> bool:
    artifact = package.split(":", 1)[1].lower() if ":" in package else ""
    return artifact == "bom" or artifact.endswith("-bom")


def gradle_bom_management_note(package: str, platforms: list[tuple[str, str, str]]) -> str:
    if ":" not in package:
        return ""
    group = package.split(":", 1)[0]
    for bom_package, bom_version, bom_alias in platforms:
        if ":" not in bom_package:
            continue
        bom_group = bom_package.split(":", 1)[0]
        if group == bom_group or group.startswith(bom_group + "."):
            return f"bom_managed_by:{bom_alias}:{bom_package}:{bom_version}"
    return ""


def extract_gradle_lib_aliases(text: str) -> set[str]:
    aliases = set()
    for match in re.finditer(r"\blibs((?:\.[A-Za-z_][A-Za-z0-9_-]*)+)", text):
        value = match.group(1).strip(".")
        if not value or value.startswith("plugins.") or value.startswith("bundles.") or value.startswith("versions."):
            continue
        aliases.add(normalize_gradle_alias(value))
    return aliases


def normalize_gradle_alias(value: str) -> str:
    return re.sub(r"[-_.]+", ".", value.strip().lower()).strip(".")


def gradle_catalog_scope(path: str) -> str:
    normalized = path.replace("\\", "/")
    suffix = "gradle/libs.versions.toml"
    if normalized.endswith(suffix):
        return normalized[: -len(suffix)]
    return ""


def gradle_same_scope(build_path: str, scope: str) -> bool:
    normalized = build_path.replace("\\", "/")
    return not scope or normalized.startswith(scope)


def parse_gradle_lockfile(path: str, text: str) -> list[dict[str, str]]:
    rows = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        coords = line.split("=", 1)[0].strip()
        parts = coords.split(":")
        if len(parts) >= 3:
            group, artifact, version = parts[0], parts[1], parts[2]
            rows.append(dep_row(path, f"{group}:{artifact}", version, "lockfile_possible_transitive", "resolved", "gradle.lockfile"))
    return rows


def parse_composer_json(path: str, text: str) -> list[dict[str, str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = []
    for section in ['require', 'require-dev', 'conflict', 'replace', 'provide']:
        deps = data.get(section) or {}
        if isinstance(deps, dict):
            for name, spec in deps.items():
                name = str(name).lower()
                if name == 'php' or name.startswith('ext-'):
                    continue
                rows.append(dep_row(path, name, str(spec), 'manifest', version_kind(str(spec)), section))
    rows.extend(parse_composer_first_party_autoload(path, data))
    return rows


def parse_composer_first_party_autoload(path: str, data: dict) -> list[dict[str, str]]:
    rows = []
    for section in ['autoload', 'autoload-dev']:
        autoload = data.get(section) or {}
        if not isinstance(autoload, dict):
            continue
        for prefix in composer_psr4_prefixes(autoload):
            rows.append(dep_row(path, prefix, '', 'first_party_module', 'non_registry', section, namespace_hint=prefix))
    return rows


def composer_psr4_prefixes(autoload: dict) -> list[str]:
    psr4 = autoload.get('psr-4') or {}
    if not isinstance(psr4, dict):
        return []
    return [str(prefix).strip(chr(92)) for prefix in psr4 if str(prefix).strip(chr(92))]


def parse_composer_lock(path: str, text: str) -> list[dict[str, str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = []
    for section in ['packages', 'packages-dev']:
        packages = data.get(section) or []
        if isinstance(packages, list):
            for item in packages:
                if isinstance(item, dict) and item.get('name') and item.get('version'):
                    package = str(item['name']).lower()
                    version = str(item['version'])
                    rows.append(dep_row(path, package, version, 'lockfile_possible_transitive', 'resolved', section))
                    autoload = item.get('autoload') or {}
                    if isinstance(autoload, dict):
                        for prefix in composer_psr4_prefixes(autoload):
                            rows.append(dep_row(path, package, version, 'lockfile_namespace_hint', 'resolved', section, namespace_hint=prefix))
    return rows


def parse_nuget_xml(path: str, text: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    rows = []
    for value in nuget_project_namespaces(root):
        rows.append(dep_row(path, value, '', 'first_party_module', 'non_registry', 'project_namespace', namespace_hint=value))
    for node in root.iter():
        tag = node.tag.split('}', 1)[-1].lower()
        if tag == 'packagereference':
            name = node.attrib.get('Include') or node.attrib.get('Update') or node.attrib.get('Remove') or ''
            version = node.attrib.get('Version') or xml_child_text(node, 'Version')
            if name:
                rows.append(dep_row(path, name, version, 'manifest', nuget_version_kind(version), 'PackageReference'))
        elif tag == 'package':
            name = node.attrib.get('id') or ''
            version = node.attrib.get('version') or ''
            if name:
                rows.append(dep_row(path, name, version, 'manifest', nuget_version_kind(version), 'packages.config'))
    return rows




def nuget_project_namespaces(root: ET.Element) -> list[str]:
    values = []
    for node in root.iter():
        tag = node.tag.split('}', 1)[-1]
        if tag in {'RootNamespace', 'AssemblyName'} and node.text:
            value = node.text.strip()
            if value and '$(' not in value:
                values.append(value)
    return sorted(set(values))


def nuget_version_kind(version: str) -> str:
    value = (version or '').strip()
    if not value:
        return 'unresolved'
    if value.startswith('$(') or value.startswith('[') or value.startswith('(') or '*' in value:
        return 'range'
    return version_kind(value)


def parse_nuget_lock(path: str, text: str) -> list[dict[str, str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = []
    deps = data.get('dependencies') or {}
    if isinstance(deps, dict):
        for target, packages in deps.items():
            if isinstance(packages, dict):
                for name, item in packages.items():
                    if isinstance(item, dict):
                        version = str(item.get('resolved') or item.get('requested') or '')
                        kind = 'resolved' if item.get('resolved') else nuget_version_kind(version)
                        rows.append(dep_row(path, str(name), version, 'lockfile_possible_transitive', kind, target))
    return rows


def parse_vcpkg_json(path: str, text: str) -> list[dict[str, str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = []
    deps = data.get('dependencies') or []
    if isinstance(deps, list):
        for dep in deps:
            if isinstance(dep, str):
                rows.append(dep_row(path, dep.lower(), '', 'manifest', 'unresolved', 'dependencies'))
            elif isinstance(dep, dict) and dep.get('name'):
                version = str(dep.get('version>=') or dep.get('version') or '')
                rows.append(dep_row(path, str(dep['name']).lower(), version, 'manifest', version_kind(version), 'dependencies'))
    return rows


def parse_conanfile(path: str, text: str) -> list[dict[str, str]]:
    rows = []
    in_requires = False
    for raw in text.splitlines():
        line = raw.strip().strip(',')
        if not line or line.startswith('#'):
            continue
        if line.lower() == '[requires]':
            in_requires = True
            continue
        if line.startswith('[') and in_requires:
            in_requires = False
        candidates = re.findall(r"['\"]?([A-Za-z0-9_.+-]+)/([A-Za-z0-9_.+-]+)(?:@[^'\"]+)?['\"]?", line if in_requires or 'requires' in line else '')
        for name, version in candidates:
            rows.append(dep_row(path, name.lower(), version, 'manifest', version_kind(version), 'requires'))
    return rows


def candidate_manifest_files(
    candidate: dict[str, str], args: argparse.Namespace, token: str,
    imports: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    changed = changed_manifest_files(candidate)
    tree: list[dict[str, str]] = []
    if args.fetch_tree and (not changed or args.tree_mode == 'merge-tree'):
        tree = tree_manifest_files(
            candidate, token, args.cache_dir, args.tree_max_files,
            {
                row.get("package_candidate") or row.get("import_root", "")
                for row in (imports or [])
            },
        )
    return changed, merge_file_rows(changed, tree)


def annotate_dependency(
    candidate: dict[str, str], dep: dict[str, str], changed_paths: set[str],
) -> dict[str, str]:
    return {
        'repo': candidate.get('repo', ''),
        'sha': candidate.get('sha', ''),
        'repo_language': candidate.get('repo_language', ''),
        'author_date': candidate.get('author_date', ''),
        'dependency_file_changed_in_diff': str(
            normalize_posix_path(dep.get('dep_file_path', '')) in changed_paths
        ).lower(),
        **dep,
    }


def parsed_file_dependencies(
    candidate: dict[str, str], path: str, text: str, changed_paths: set[str],
) -> list[dict[str, str]]:
    language = candidate.get('repo_language', '')
    rows = [
        annotate_dependency(candidate, dep, changed_paths)
        for dep in parse_dependency_file(language, path, text)
    ]
    if language == 'Go' and Path(path).name.lower() == 'go.mod':
        module = parse_go_module_path(text)
        if module:
            rows.append(annotate_dependency(
                candidate,
                dep_row(path, module, '', 'first_party_module', 'unresolved', 'module'),
                changed_paths,
            ))
    return rows


def fetch_candidate_file(
    candidate: dict[str, str], file_row: dict[str, str], token: str,
    args: argparse.Namespace, changed_paths: set[str],
) -> tuple[dict[str, str], list[dict[str, str]], dict[str, str] | None]:
    if file_row.get('discovered_by') == 'tree_error':
        row = fetch_row(candidate, '', '', 'tree_error', 'tree', '', file_row.get('error', ''))
        return row, [], None
    status, text, evidence = fetch_file(file_row, candidate, token, args.cache_dir)
    row = fetch_row(candidate, file_row['path'], file_row['role'], status, file_row['discovered_by'], evidence, '')
    if status != 'ok':
        return row, [], None
    deps = parsed_file_dependencies(candidate, file_row['path'], text, changed_paths)
    return row, deps, {'path': file_row['path'], 'text': text}


def process_candidate(
    candidate: dict[str, str], args: argparse.Namespace, token: str,
    imports: list[dict[str, str]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    changed, files = candidate_manifest_files(candidate, args, token, imports)
    if not files:
        status = 'tree_manifest_absent' if args.fetch_tree else 'no_changed_manifest_in_diff'
        source = 'tree' if args.fetch_tree else 'changed_files'
        return {'fetch': [fetch_row(candidate, '', '', status, source, '', '')], 'deps': [], 'maven': []}
    changed_paths = {normalize_posix_path(item.get('path', '')) for item in changed}
    fetch_rows: list[dict[str, str]] = []
    dep_rows: list[dict[str, str]] = []
    fetched_texts: list[dict[str, str]] = []
    for file_row in files:
        fetch_result, deps, fetched = fetch_candidate_file(candidate, file_row, token, args, changed_paths)
        fetch_rows.append(fetch_result)
        dep_rows.extend(deps)
        if fetched:
            fetched_texts.append(fetched)
        time.sleep(args.sleep_seconds)
    catalog = build_gradle_catalog_usage_rows(candidate.get('repo_language', ''), fetched_texts)
    dep_rows.extend(annotate_dependency(candidate, dep, changed_paths) for dep in catalog)
    dep_rows, managed = enrich_maven_managed_versions(
        candidate.get('repo_language', ''), dep_rows, fetched_texts, args.cache_dir
    )
    return {'fetch': fetch_rows, 'deps': dep_rows, 'maven': managed}


def run_stage2_fetch(
    args: argparse.Namespace, token: str,
) -> tuple[Stage2AppendStore, dict[str, object]]:
    store = Stage2AppendStore(
        args.output_dir, stage2_fetch_specs(), resume=args.resume_stage2
    )
    candidate_path = args.input_dir / 'candidate_commits.csv'
    import_path = args.input_dir / 'import_events.csv'
    prepared = store.prepare()
    resume_after = int(prepared.get('processed_candidate_commits', 0) or 0)
    with OrderedCsvGroupCursor(import_path, ('repo', 'sha')) as imports:
        advance_import_cursor(imports, candidate_path, resume_after)
        state = stream_candidate_batches(
            candidate_path,
            store,
            lambda candidate: process_candidate(
                candidate, args, token,
                imports.take((candidate.get('repo', ''), candidate.get('sha', ''))),
            ),
            max_commits=args.max_commits,
            checkpoint_every=args.checkpoint_every_commits,
            max_buffer_rows=args.checkpoint_max_buffer_rows,
        )
    print(
        f"stage2_fetch processed_candidate_commits={state.get('processed_candidate_commits', 0)} "
        f"manifest_fetch_rows={state.get('manifest_fetch_rows', 0)} "
        f"parsed_dependencies={state.get('parsed_dependencies', 0)}",
        flush=True,
    )
    return store, state


def advance_import_cursor(
    cursor: OrderedCsvGroupCursor, candidate_path: Path, count: int,
) -> None:
    for index, candidate in enumerate(iter_csv_rows(candidate_path)):
        if index >= count:
            break
        cursor.take((candidate.get('repo', ''), candidate.get('sha', '')))


def build_python_wheel_import_map(dep_rows: list[dict[str, str]], args: argparse.Namespace) -> tuple[dict[tuple[str, str, str], list[dict[str, str]]], list[dict[str, str]]]:
    if args.python_wheel_map == "off":
        return {}, []
    cache_path = args.cache_dir / "python_wheel_import_map.csv"
    cached = load_wheel_cache(cache_path)
    wheel_map, output_rows, new_packages = build_python_wheel_group(
        dep_rows, args, cached
    )
    if new_packages:
        write_wheel_cache(cache_path, cached)
    return wheel_map, output_rows


def build_python_wheel_group(
    dep_rows: list[dict[str, str]], args: argparse.Namespace,
    cached: dict[tuple[str, str], list[dict[str, str]]],
) -> tuple[dict[tuple[str, str, str], list[dict[str, str]]], list[dict[str, str]], int]:
    if args.python_wheel_map == 'off':
        return {}, [], 0
    new_packages = fetch_missing_wheel_packages(dep_rows, args, cached)
    wheel_map, output_rows = wheel_rows_for_dependencies(dep_rows, cached)
    return wheel_map, output_rows, new_packages


def fetch_missing_wheel_packages(
    dep_rows: list[dict[str, str]], args: argparse.Namespace,
    cached: dict[tuple[str, str], list[dict[str, str]]],
) -> int:
    needed = sorted({
        (dep.get("package_name", ""), python_dep_version(dep))
        for dep in dep_rows
        if dep.get("repo_language") == "Python" and dep.get("package_name") and python_dep_version(dep) and dep.get("version_kind") in {"exact", "resolved"}
    })
    fetched_count = 0
    for package, version in needed:
        if (package.lower(), version) in cached:
            continue
        fetched = fetch_wheel_import_roots(package, version, args.cache_dir, args.wheel_size_limit_mb)
        cached[(package.lower(), version)] = fetched
        fetched_count += 1
        if args.wheel_sleep_seconds:
            time.sleep(args.wheel_sleep_seconds)
    return fetched_count


def wheel_rows_for_dependencies(
    dep_rows: list[dict[str, str]],
    cached: dict[tuple[str, str], list[dict[str, str]]],
) -> tuple[dict[tuple[str, str, str], list[dict[str, str]]], list[dict[str, str]]]:
    output_rows: list[dict[str, str]] = []
    wheel_map: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for dep in dep_rows:
        if dep.get("repo_language") != "Python":
            continue
        version = python_dep_version(dep)
        if not version:
            continue
        rows = cached.get((dep.get("package_name", "").lower(), version), [])
        for item in rows:
            row = {
                "repo": dep.get("repo", ""),
                "sha": dep.get("sha", ""),
                "package_name": dep.get("package_name", ""),
                "version": version,
                "import_root": item.get("import_root", ""),
                "status": item.get("status", ""),
                "source": item.get("source", ""),
                "wheel_filename": item.get("wheel_filename", ""),
                "wheel_size": item.get("wheel_size", ""),
                "error": item.get("error", ""),
            }
            output_rows.append(row)
            if row["status"] == "ok" and row["import_root"]:
                wheel_map.setdefault((row["repo"], row["sha"], row["import_root"]), []).append(dep)
    return wheel_map, output_rows


def python_dep_version(dep: dict[str, str]) -> str:
    if dep.get("resolved_version"):
        return dep["resolved_version"].strip()
    if dep.get("version_kind") == "exact":
        return clean_exact_version(dep.get("version_spec", ""))
    return ""


def load_wheel_cache(path: Path) -> dict[tuple[str, str], list[dict[str, str]]]:
    rows: dict[tuple[str, str], list[dict[str, str]]] = {}
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(line.replace("\x00", "") for line in handle):
            rows.setdefault((row.get("package_name", "").lower(), row.get("version", "")), []).append(row)
    return rows


def write_wheel_cache(path: Path, cached: dict[tuple[str, str], list[dict[str, str]]]) -> None:
    rows = []
    seen = set()
    for (_package, _version), values in cached.items():
        for row in values:
            key = tuple(row.get(field, "") for field in ["package_name", "version", "import_root", "status", "source", "wheel_filename", "error"])
            if key not in seen:
                rows.append(row)
                seen.add(key)
    write_csv(path, rows, ["package_name", "version", "import_root", "status", "source", "wheel_filename", "wheel_size", "error"])


def fetch_wheel_import_roots(package: str, version: str, cache_dir: Path, size_limit_mb: float) -> list[dict[str, str]]:
    base = {"package_name": package, "version": version, "import_root": "", "status": "", "source": "", "wheel_filename": "", "wheel_size": "", "error": ""}
    url = f"https://pypi.org/pypi/{urllib.parse.quote(package)}/{urllib.parse.quote(version)}/json"
    status, text, error = request_text(url, "")
    if status != 200:
        return [{**base, "status": f"pypi_json_error_{status}", "error": error}]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [{**base, "status": "pypi_json_decode_error", "error": str(exc)}]
    wheels = [item for item in data.get("urls", []) if item.get("packagetype") == "bdist_wheel" and item.get("url")]
    if not wheels:
        return [{**base, "status": "no_wheel", "error": "no bdist_wheel file for exact version"}]
    limit = int(size_limit_mb * 1024 * 1024)
    eligible = [item for item in wheels if int(item.get("size") or 0) <= limit]
    if not eligible:
        smallest = min((int(item.get("size") or 0) for item in wheels), default=0)
        return [{**base, "status": "wheel_too_large", "wheel_size": str(smallest), "error": f"all wheels exceed {size_limit_mb} MB"}]
    wheel = sorted(eligible, key=lambda item: int(item.get("size") or 0))[0]
    wheel_url = wheel["url"]
    wheel_name = str(wheel.get("filename") or safe_name(package + "-" + version) + ".whl")
    tmp_dir = cache_dir / "wheel_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / safe_name(wheel_name)
    try:
        download_file(wheel_url, tmp_path)
        roots = extract_import_roots_from_wheel(tmp_path)
        if not roots:
            return [{**base, "status": "no_import_roots", "source": "wheel_metadata", "wheel_filename": wheel_name, "wheel_size": str(wheel.get("size", ""))}]
        return [
            {**base, "import_root": root, "status": "ok", "source": "wheel_metadata", "wheel_filename": wheel_name, "wheel_size": str(wheel.get("size", ""))}
            for root in sorted(roots)
        ]
    except (OSError, zipfile.BadZipFile, urllib.error.URLError) as exc:
        return [{**base, "status": "wheel_read_error", "source": "wheel_metadata", "wheel_filename": wheel_name, "wheel_size": str(wheel.get("size", "")), "error": str(exc)}]
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def download_file(url: str, path: Path) -> None:
    headers = {"User-Agent": "rq3-hallucinated-dependency-probe"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        path.write_bytes(response.read())


def extract_import_roots_from_wheel(path: Path) -> set[str]:
    roots: set[str] = set()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for name in names:
            if name.endswith(".dist-info/top_level.txt"):
                text = archive.read(name).decode("utf-8", errors="replace")
                roots.update(root for root in (line.strip() for line in text.splitlines()) if valid_python_root(root))
        if roots:
            return roots
        for name in names:
            if ".dist-info/" in name or ".data/" in name or not name:
                continue
            first = name.split("/", 1)[0]
            if first.endswith(".py"):
                first = first[:-3]
            if valid_python_root(first):
                roots.add(first)
    return roots


def valid_python_root(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_]\w*", value or ""))


def merge_file_rows(changed_files: list[dict[str, str]], tree_files: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = []
    seen = set()
    for item in changed_files + tree_files:
        key = item.get("path", "")
        if key and key not in seen:
            rows.append(item)
            seen.add(key)
        elif item.get("discovered_by") == "tree_error":
            rows.append(item)
    return rows


def fetch_row(candidate: dict[str, str], path: str, role: str, status: str, discovered_by: str, evidence: str, error: str) -> dict[str, str]:
    return {"repo": candidate.get("repo", ""), "sha": candidate.get("sha", ""), "child_sha": candidate.get("sha", ""), "repo_language": candidate.get("repo_language", ""), "manifest_path": path, "role": role, "fetch_status": status, "discovered_by": discovered_by, "evidence_path_or_url": evidence, "error": error}


def match_imports(
    imports: list[dict[str, str]],
    deps: list[dict[str, str]],
    wheel_map: dict[tuple[str, str, str], list[dict[str, str]]] | None = None,
    local_map: set[tuple[str, str, str]] | None = None,
    jvm_local_map: set[tuple[str, str, str]] | None = None,
) -> list[dict[str, str]]:
    dep_map = {}
    for dep in deps:
        dep_map.setdefault((dep["repo"], dep["sha"]), []).append(dep)
    rows = []
    for event in imports:
        if event.get("import_kind") != "external_candidate":
            continue
        match = best_match(event, dep_map.get((event["repo"], event["sha"]), []), wheel_map or {}, local_map or set(), jvm_local_map or set())
        rows.append({**event, **match})
    return rows


def best_match(
    event: dict[str, str],
    deps: list[dict[str, str]],
    wheel_map: dict[tuple[str, str, str], list[dict[str, str]]] | None = None,
    local_map: set[tuple[str, str, str]] | None = None,
    jvm_local_map: set[tuple[str, str, str]] | None = None,
) -> dict[str, str]:
    package = event.get("package_candidate", "")
    if not package:
        inferred = best_namespace_match(event, deps, jvm_local_map or set())
        if inferred:
            return inferred
        return match_row("mapping_unknown", "", "", "", "")
    if event.get("ecosystem") == "Go":
        first_party = [dep for dep in deps if dep.get("resolution_source") == "first_party_module"]
        if first_party and any(package == dep["package_name"] or package.startswith(dep["package_name"] + "/") for dep in first_party):
            return match_from_dep("first_party_module_import", first_party[0])
        matches = [dep for dep in deps if package == dep["package_name"] or package.startswith(dep["package_name"] + "/")]
        if matches:
            match = sorted(matches, key=lambda item: len(item["package_name"]), reverse=True)[0]
            return match_from_dep(label_for_match([match]), match)
    if event.get("ecosystem") == "npm":
        first_party = [
            dep for dep in deps
            if dep.get("resolution_source") == "first_party_module"
            and package.lower() == dep.get("package_name", "").lower()
        ]
        if first_party:
            return match_from_dep("first_party_module_import", first_party[0])
    matches = [dep for dep in deps if package.lower() == dep["package_name"].lower()]
    if matches:
        return match_from_deps(matches)
    if event.get("ecosystem") == "PyPI":
        inferred = best_python_wheel_match(event, wheel_map or {})
        if inferred:
            return inferred
        if is_python_local_module(event, local_map or set()):
            return match_row("first_party_module_import", package, "", "", "python_local_module", "non_registry", "local_source_tree", "", package)
    if deps:
        return match_row("not_observed_in_parsed_dependency_files", "", "", "", "")
    return match_row("cannot_compare_no_parsed_dependencies", "", "", "", "")



COMMON_PYTHON_CONTAINER_DIRS = {"src", "lib", "libs", "backend", "server", "service", "services"}


def build_python_local_module_map(imports: list[dict[str, str]]) -> tuple[set[tuple[str, str, str]], list[dict[str, str]]]:
    roots: dict[tuple[str, str], set[str]] = {}
    evidence: dict[tuple[str, str, str], set[str]] = {}
    for event in imports:
        if event.get("ecosystem") != "PyPI":
            continue
        source = (event.get("source_file") or "").replace("\\", "/")
        if not source.endswith(".py"):
            continue
        repo_sha = (event.get("repo", ""), event.get("sha", ""))
        for root in local_roots_from_source_path(source):
            roots.setdefault(repo_sha, set()).add(root)
            evidence.setdefault((repo_sha[0], repo_sha[1], root), set()).add(source)
    keys = {(repo, sha, root) for (repo, sha), values in roots.items() for root in values}
    rows = [
        {"repo": repo, "sha": sha, "import_root": root, "source_paths": ";".join(sorted(evidence.get((repo, sha, root), set()))[:20]), "status": "ok"}
        for repo, sha, root in sorted(keys)
    ]
    return keys, rows


def local_roots_from_source_path(source: str) -> set[str]:
    path = source.strip("/")
    if not path or path.startswith((".", "/")):
        return set()
    parts = [part for part in path.split("/") if part]
    if not parts:
        return set()
    filename = parts[-1]
    roots: set[str] = set()
    if filename.endswith(".py") and filename != "__init__.py":
        stem = filename[:-3]
        if valid_python_root(stem):
            roots.add(stem)
    package_parts = parts[:-1]
    if package_parts:
        first = package_parts[0]
        if valid_python_root(first) and first not in COMMON_PYTHON_CONTAINER_DIRS:
            roots.add(first)
        if first in COMMON_PYTHON_CONTAINER_DIRS and len(package_parts) > 1 and valid_python_root(package_parts[1]):
            roots.add(package_parts[1])
    return roots


def is_python_local_module(event: dict[str, str], local_map: set[tuple[str, str, str]]) -> bool:
    root = (event.get("import_root") or event.get("import_raw", "").split(".")[0]).replace("-", "_")
    return bool(root and (event.get("repo", ""), event.get("sha", ""), root) in local_map)


JVM_SOURCE_DIRS = {"java", "kotlin", "scala", "groovy"}


def build_jvm_local_package_map(imports: list[dict[str, str]]) -> tuple[set[tuple[str, str, str]], list[dict[str, str]]]:
    roots: dict[tuple[str, str], set[str]] = {}
    evidence: dict[tuple[str, str, str], set[str]] = {}
    for event in imports:
        if event.get("ecosystem") != "Maven":
            continue
        source = (event.get("source_file") or "").replace("\\", "/")
        local_root = jvm_package_root_from_source_path(source)
        if not local_root:
            continue
        repo_sha = (event.get("repo", ""), event.get("sha", ""))
        roots.setdefault(repo_sha, set()).add(local_root)
        evidence.setdefault((repo_sha[0], repo_sha[1], local_root), set()).add(source)
    keys = {(repo, sha, root) for (repo, sha), values in roots.items() for root in values}
    rows = [
        {"repo": repo, "sha": sha, "package_root": root, "source_paths": ";".join(sorted(evidence.get((repo, sha, root), set()))[:20]), "status": "ok"}
        for repo, sha, root in sorted(keys)
    ]
    return keys, rows


def jvm_package_root_from_source_path(source: str) -> str:
    path = source.strip("/")
    if not path:
        return ""
    parts = [part for part in path.split("/") if part]
    if len(parts) < 4:
        return ""
    filename = parts[-1]
    if not filename.endswith((".java", ".kt", ".kts", ".scala", ".groovy")):
        return ""
    package_parts = parts[:-1]
    for index, part in enumerate(package_parts):
        if part not in JVM_SOURCE_DIRS:
            continue
        candidates = package_parts[index + 1:]
        if len(candidates) >= 3 and all(valid_jvm_package_part(item) for item in candidates[:3]):
            return ".".join(candidates[:3])
    return ""


def valid_jvm_package_part(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value or ""))


def is_jvm_local_package(event: dict[str, str], jvm_local_map: set[tuple[str, str, str]]) -> bool:
    root = event.get("import_root", "") or event.get("import_raw", "")
    return bool(root and (event.get("repo", ""), event.get("sha", ""), root) in jvm_local_map)


def best_python_wheel_match(event: dict[str, str], wheel_map: dict[tuple[str, str, str], list[dict[str, str]]]) -> dict[str, str] | None:
    root = (event.get("import_root") or event.get("import_raw", "").split(".")[0]).replace("-", "_")
    if not root:
        return None
    matches = wheel_map.get((event.get("repo", ""), event.get("sha", ""), root), [])
    if not matches:
        return None
    names = {item.get("package_name", "").lower() for item in matches}
    if len(names) != 1:
        return None
    resolved = [item for item in matches if item.get("resolved_version")]
    dep = resolved[0] if resolved else matches[0]
    return match_from_dep("observed_in_python_wheel_metadata", dep)


def best_namespace_match(event: dict[str, str], deps: list[dict[str, str]], jvm_local_map: set[tuple[str, str, str]] | None = None) -> dict[str, str] | None:
    ecosystem = event.get("ecosystem", "")
    root = event.get("import_root", "") or event.get("import_raw", "")
    if not root:
        return None
    if ecosystem in {"PHP", "Packagist"}:
        first_party = namespace_hint_matches(root.replace('\\', '\\'), deps, {'first_party_module'})
        if first_party:
            return match_from_dep('first_party_module_import', first_party[0])
        matches = namespace_hint_matches(root.replace('\\', '\\'), deps, {'lockfile_namespace_hint'})
        return unique_namespace_match(matches, "namespace_lockfile_match")
    if ecosystem == "Maven":
        if is_jvm_local_package(event, jvm_local_map or set()):
            return match_row("first_party_module_import", root, "", "", "jvm_local_package", "non_registry", "local_source_tree", "", root)
        if not deps:
            return None
        matches = []
        for dep in deps:
            if gradle_dependency_outside_source_module(event, dep):
                continue
            name = dep.get("package_name", "")
            if ":" not in name:
                continue
            group, artifact = name.split(":", 1)
            if artifact.lower() == "bom" or artifact.lower().endswith("-bom"):
                continue
            if root == group or root.startswith(group + "."):
                matches.append(dep)
        return unique_namespace_match(matches, "namespace_manifest_match")
    if ecosystem == "NuGet":
        first_party = namespace_hint_matches(root, deps, {'first_party_module'})
        if first_party:
            return match_from_dep('first_party_module_import', first_party[0])
        lowered = root.lower()
        matches = []
        for dep in deps:
            name = dep.get("package_name", "")
            dep_lower = name.lower()
            if lowered == dep_lower or lowered.startswith(dep_lower + "."):
                matches.append(dep)
        return unique_namespace_match(matches, "namespace_manifest_match")
    return None




def gradle_dependency_outside_source_module(event: dict[str, str], dep: dict[str, str]) -> bool:
    dep_path = (dep.get("dep_file_path") or "").replace("\\", "/")
    base = Path(dep_path).name.lower()
    if base not in {"build.gradle", "build.gradle.kts"}:
        return False
    module_scope = dep_path[: -len(base)].strip("/")
    if not module_scope:
        return False
    source = (event.get("source_file") or "").replace("\\", "/").strip("/")
    return bool(source and not source.startswith(module_scope + "/"))


def namespace_hint_matches(root: str, deps: list[dict[str, str]], sources: set[str]) -> list[dict[str, str]]:
    normalized_root = root.strip('\\.')
    matches = []
    for dep in deps:
        if dep.get('resolution_source') not in sources:
            continue
        hint = dep.get('namespace_hint', '').strip('\\.')
        if not hint:
            continue
        if normalized_root == hint or normalized_root.startswith(hint + '\\') or normalized_root.startswith(hint + '.'):
            matches.append(dep)
    return matches


def unique_namespace_match(matches: list[dict[str, str]], label: str) -> dict[str, str] | None:
    if not matches:
        return None
    names = {item.get("package_name", "").lower() for item in matches}
    if len(names) != 1:
        return None
    return match_from_deps(matches)

def label_for_match(matches: list[dict[str, str]]) -> str:
    sources = {item.get("resolution_source", "") for item in matches}
    manifest_sources = {"manifest", "manifest_npm_alias"}
    if sources & manifest_sources and "lockfile_possible_transitive" in sources:
        return "observed_in_manifest_and_lockfile_resolved"
    if sources & manifest_sources:
        return "observed_in_parsed_manifest"
    if "lockfile_possible_transitive" in sources or "lockfile_namespace_hint" in sources:
        return "observed_in_lockfile_possible_transitive"
    return "observed_in_parsed_dependency_files"


def match_from_deps(matches: list[dict[str, str]]) -> dict[str, str]:
    manifest_matches = [
        dep for dep in matches
        if dep.get("resolution_source") in {"manifest", "manifest_npm_alias"}
    ]
    resolved_matches = [dep for dep in matches if dep.get("resolved_version")]
    primary_candidates = manifest_matches or matches
    primary = sorted(primary_candidates, key=is_gradle_catalog_dep)[0]
    resolved = resolved_matches[0] if resolved_matches else primary

    return match_row(
        label_for_match(matches),
        primary.get("package_name", ""),
        primary.get("version_spec", ""),
        resolved.get("resolved_version", ""),
        "manifest+lockfile" if primary is not resolved and resolved.get("resolved_version") else primary.get("resolution_source", ""),
        "resolved" if resolved.get("resolved_version") else primary.get("version_kind", ""),
        primary.get("dependency_group", ""),
        primary.get("dep_file_path", ""),
        primary.get("namespace_hint", ""),
        primary.get("dependency_file_changed_in_diff", ""),
    )


def is_gradle_catalog_dep(dep: dict[str, str]) -> bool:
    return str(dep.get("dependency_group", "")).startswith("gradle.version_catalog:")


def match_from_dep(label: str, dep: dict[str, str]) -> dict[str, str]:
    return match_row(
        label,
        dep.get("package_name", ""),
        dep.get("version_spec", ""),
        dep.get("resolved_version", ""),
        dep.get("resolution_source", ""),
        dep.get("version_kind", ""),
        dep.get("dependency_group", ""),
        dep.get("dep_file_path", ""),
        dep.get("namespace_hint", ""),
        dep.get("dependency_file_changed_in_diff", ""),
    )


def match_row(
    label: str,
    declared: str,
    spec: str,
    resolved: str,
    source: str,
    version_kind: str = "",
    dependency_group: str = "",
    dep_file_path: str = "",
    namespace_hint: str = "",
    dependency_file_changed_in_diff: str = "",
) -> dict[str, str]:
    return {
        "declared_dependency_match": label,
        "declared_package": declared,
        "version_spec": spec,
        "resolved_version": resolved,
        "resolution_source": source,
        "version_kind": version_kind,
        "dependency_group": dependency_group,
        "dep_file_path": dep_file_path,
        "namespace_hint": namespace_hint,
        "dependency_file_changed_in_diff": dependency_file_changed_in_diff,
    }


class Stage2MatchProcessor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.cache_path = args.cache_dir / 'python_wheel_import_map.csv'
        self.wheel_cache = (
            load_wheel_cache(self.cache_path)
            if args.python_wheel_map != 'off'
            else {}
        )
        self.pending_wheel_packages = 0

    def __call__(
        self, _candidate: dict[str, str], imports: list[dict[str, str]],
        deps: list[dict[str, str]],
    ) -> dict[str, list[dict[str, str]]]:
        wheel_map, wheel_rows, new_packages = build_python_wheel_group(
            deps, self.args, self.wheel_cache
        )
        self.pending_wheel_packages += new_packages
        self.flush_wheel_cache_if_due()
        local_map, local_rows = build_python_local_module_map(imports)
        jvm_map, jvm_rows = build_jvm_local_package_map(imports)
        matches = match_imports(imports, deps, wheel_map, local_map, jvm_map)
        return {
            'matches': matches,
            'wheel': wheel_rows,
            'python_local': local_rows,
            'jvm_local': jvm_rows,
        }

    def flush_wheel_cache_if_due(self, *, force: bool = False) -> None:
        every = max(0, int(self.args.checkpoint_every_wheel_packages or 0))
        if not self.pending_wheel_packages:
            return
        if not force and (every <= 0 or self.pending_wheel_packages < every):
            return
        write_wheel_cache(self.cache_path, self.wheel_cache)
        self.pending_wheel_packages = 0


def run_stage2_matching(args: argparse.Namespace) -> dict[str, int]:
    processor = Stage2MatchProcessor(args)
    counts = stream_grouped_outputs(
        args.input_dir / 'candidate_commits.csv',
        args.input_dir / 'import_events.csv',
        args.output_dir / 'parsed_dependencies.csv',
        args.output_dir,
        stage2_match_specs(),
        processor,
        max_commits=args.max_commits,
    )
    processor.flush_wheel_cache_if_due(force=True)
    return counts


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    token = load_token(args.github_token_env)
    store, state = run_stage2_fetch(args, token)
    if state.get('complete'):
        print(f"stage2_already_complete output_dir={args.output_dir}")
        return
    match_counts = run_stage2_matching(args)
    state = store.mark_complete()
    print(f"manifest_fetch_rows={state.get('manifest_fetch_rows', 0)}")
    print(f"parsed_dependencies={state.get('parsed_dependencies', 0)}")
    print(f"maven_managed_version_rows={state.get('maven_managed_version_rows', 0)}")
    print(f"python_wheel_import_map_rows={match_counts.get('wheel', 0)}")
    print(f"python_local_module_map_rows={match_counts.get('python_local', 0)}")
    print(f"jvm_local_package_map_rows={match_counts.get('jvm_local', 0)}")
    print(f"import_matches={match_counts.get('matches', 0)}")
    print(f"output_dir={args.output_dir}")


if __name__ == "__main__":
    main()



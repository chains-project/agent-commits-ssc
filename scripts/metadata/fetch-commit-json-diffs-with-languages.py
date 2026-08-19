"""
[IN]: merged commit population CSV, repo_metadata.csv, GITHUB_TOKEN, and target language filters.
[OUT]: <THESIS_DATA_ROOT>/diff_corpus/commit_json_diffs_by_language/<Language>/*.json plus manifest/index CSV files.
[POS]: Builds commit JSON diff corpus with repo/file/commit language annotations for RQ3 patch-level analysis.
[SYNC]: If language inference, pagination, output root, or manifest schema changes, update scripts/CLAUDE.md and scripts/OUTPUTS.md.
Fetch commit JSON diffs and infer changed-file languages.

This collector uses GitHub's "Get a commit" JSON response instead of the raw
diff media type. One normal request returns commit metadata, stats, and a
files[] list with per-file status/additions/deletions/patch. When a commit
touches many files, the GitHub API paginates the files[] list; this script
follows those pages and merges the file list into one saved JSON document.

Language fields:
  - repo_language: GitHub repository primary language already joined into the
    commit CSV, or joined from repo metadata as a fallback.
  - file_language: inferred from the changed file path using a Linguist-inspired
    filename/extension table. This is not a full GitHub Linguist run because
    exact Linguist classification can depend on file contents and .gitattributes.
  - commit_languages: distinct file_language values changed by the commit.
  - commit_primary_language: changed-file language with the largest additions +
    deletions in this commit.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMMIT_CSV = (
    ROOT
    / "data_products"
    / "agent_commit_population_merged_1y_v1"
    / "merged_agent_commit_population.csv"
)
DEFAULT_REPO_METADATA_CSV = (
    ROOT / "data_products" / "repo_metadata_merged_1y_v1" / "repo_metadata.csv"
)
DEFAULT_DATA_ROOT = Path(os.environ.get("THESIS_DATA_ROOT", "data_external"))
DEFAULT_OUTPUT_ROOT = (
    DEFAULT_DATA_ROOT / "diff_corpus" / "commit_json_diffs_by_language"
)
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


FILENAME_LANGUAGES = {
    "dockerfile": "Dockerfile",
    "containerfile": "Dockerfile",
    "makefile": "Makefile",
    "gnumakefile": "Makefile",
    "cmakelists.txt": "CMake",
    "rakefile": "Ruby",
    "gemfile": "Ruby",
    "podfile": "Ruby",
    "vagrantfile": "Ruby",
    "procfile": "Procfile",
    "jenkinsfile": "Groovy",
    "justfile": "Just",
    "earthfile": "Earthly",
}

EXTENSION_LANGUAGES = {
    ".ts": "TypeScript",
    ".tsx": "TSX",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".py": "Python",
    ".pyw": "Python",
    ".ipynb": "Jupyter Notebook",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".go": "Go",
    ".rs": "Rust",
    ".c": "C",
    ".h": "C/C++ Header",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".fs": "F#",
    ".php": "PHP",
    ".rb": "Ruby",
    ".swift": "Swift",
    ".dart": "Dart",
    ".scala": "Scala",
    ".r": "R",
    ".R": "R",
    ".jl": "Julia",
    ".lua": "Lua",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hrl": "Erlang",
    ".hs": "Haskell",
    ".clj": "Clojure",
    ".cljs": "Clojure",
    ".fsx": "F#",
    ".zig": "Zig",
    ".nim": "Nim",
    ".sol": "Solidity",
    ".move": "Move",
    ".lean": "Lean",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "Sass",
    ".less": "Less",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".astro": "Astro",
    ".mdx": "MDX",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".json": "JSON",
    ".jsonc": "JSON with Comments",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".xml": "XML",
    ".sql": "SQL",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".fish": "fish",
    ".ps1": "PowerShell",
    ".psm1": "PowerShell",
    ".bat": "Batchfile",
    ".cmd": "Batchfile",
    ".dockerfile": "Dockerfile",
    ".tf": "HCL",
    ".tfvars": "HCL",
    ".hcl": "HCL",
    ".nix": "Nix",
    ".tex": "TeX",
    ".bib": "BibTeX",
    ".proto": "Protocol Buffer",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch commit JSON patches and infer changed-file languages."
    )
    parser.add_argument("--commit-csv", type=Path, default=DEFAULT_COMMIT_CSV)
    parser.add_argument("--repo-metadata-csv", type=Path, default=DEFAULT_REPO_METADATA_CSV)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--languages",
        nargs="*",
        default=None,
        help="Optional repo_language filter. Omit to fetch all repository languages.",
    )
    parser.add_argument("--max-commits", type=int, default=0)
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--retry-base-seconds", type=int, default=60)
    parser.add_argument("--rate-limit-floor", type=int, default=50)
    parser.add_argument("--rate-limit-buffer-seconds", type=int, default=30)
    parser.add_argument("--progress-interval", type=int, default=100)
    parser.add_argument("--error-cooldown-hours", type=float, default=168.0)
    parser.add_argument(
        "--permanent-error-statuses",
        default="404,409,422",
        help="Comma-separated HTTP statuses to skip on resume after a previous error.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-skipped", action="store_true")
    parser.add_argument(
        "--json-mode",
        choices=["delta", "compact", "full", "none"],
        default="delta",
        help="delta saves only Get-Commit-only diff metadata; compact removes duplicated files[].patch text; full saves raw JSON; none saves only CSV and .patch.",
    )
    parser.add_argument(
        "--no-save-json",
        action="store_true",
        help="Deprecated alias for --json-mode none.",
    )
    parser.add_argument(
        "--dedupe-key",
        choices=["repo_sha", "sha"],
        default="repo_sha",
        help="repo_sha preserves repository context; sha fetches one representative repo per unique commit object.",
    )
    return parser.parse_args()


def load_token() -> str:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "GITHUB_TOKEN":
                return value.strip().strip('"').strip("'")
    return os.environ.get("GITHUB_TOKEN", "")


def safe_segment(value: str) -> str:
    value = value or "_blank"
    value = re.sub(r'[<>:"/\\|?*]', "_", value)
    value = re.sub(r"\s+", "_", value).strip(" ._")
    return value or "_blank"


def append_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def iter_existing_index_rows(path: Path) -> Any:
    with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as handle:
        clean_lines = (line.replace("\x00", "") for line in handle)
        yield from csv.DictReader(clean_lines)


def parse_status_set(value: str) -> set[int]:
    statuses: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            statuses.add(int(part))
        except ValueError:
            continue
    return statuses


def parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def print_progress(
    fetched: int,
    skipped: int,
    error_skipped: int,
    errors: int,
    matched: int,
    started_at: float,
) -> None:
    elapsed = max(1.0, time.time() - started_at)
    processed = fetched + skipped + error_skipped + errors
    rate = processed / elapsed * 3600
    print(
        "==== progress "
        f"fetched={fetched} skipped_ok={skipped} skipped_error={error_skipped} "
        f"errors={errors} matched={matched} processed={processed} "
        f"rate_per_hour={rate:.0f} ====",
        flush=True,
    )


def read_repo_language_fallback(path: Path) -> dict[str, str]:
    languages: dict[str, str] = {}
    if not path.exists():
        return languages
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            repo = (row.get("repo") or "").strip()
            if repo:
                languages[repo] = (row.get("language") or "").strip()
    return languages


def detect_file_language(filename: str) -> tuple[str, str]:
    name = Path(filename).name
    lower_name = name.lower()
    if lower_name in FILENAME_LANGUAGES:
        return FILENAME_LANGUAGES[lower_name], "filename"

    suffixes = Path(filename).suffixes
    if suffixes:
        compound = "".join(suffixes[-2:]).lower()
        if compound in EXTENSION_LANGUAGES:
            return EXTENSION_LANGUAGES[compound], "extension"
        ext = suffixes[-1]
        if ext in EXTENSION_LANGUAGES:
            return EXTENSION_LANGUAGES[ext], "extension"
        lower_ext = ext.lower()
        if lower_ext in EXTENSION_LANGUAGES:
            return EXTENSION_LANGUAGES[lower_ext], "extension"

    return "", "unknown"


def classify_supply_chain_file(filename: str) -> dict[str, str]:
    path = filename.replace("\\", "/")
    name = Path(path).name
    lower_path = path.lower()
    lower_name = name.lower()

    exact = {
        "package.json": ("dependency_manifest", "JavaScript/TypeScript", "npm", "direct_dependencies"),
        "package-lock.json": ("lockfile", "JavaScript/TypeScript", "npm", "resolved_dependencies"),
        "npm-shrinkwrap.json": ("lockfile", "JavaScript/TypeScript", "npm", "resolved_dependencies"),
        "yarn.lock": ("lockfile", "JavaScript/TypeScript", "yarn", "resolved_dependencies"),
        "pnpm-lock.yaml": ("lockfile", "JavaScript/TypeScript", "pnpm", "resolved_dependencies"),
        "bun.lock": ("lockfile", "JavaScript/TypeScript", "bun", "resolved_dependencies"),
        "bun.lockb": ("lockfile", "JavaScript/TypeScript", "bun", "resolved_dependencies"),
        "requirements.txt": ("dependency_manifest", "Python", "pip", "direct_dependencies"),
        "requirements-dev.txt": ("dependency_manifest", "Python", "pip", "direct_dependencies"),
        "pyproject.toml": ("dependency_manifest", "Python", "python-packaging", "project_dependencies"),
        "poetry.lock": ("lockfile", "Python", "poetry", "resolved_dependencies"),
        "pipfile": ("dependency_manifest", "Python", "pipenv", "direct_dependencies"),
        "pipfile.lock": ("lockfile", "Python", "pipenv", "resolved_dependencies"),
        "setup.py": ("dependency_manifest", "Python", "setuptools", "project_dependencies"),
        "setup.cfg": ("dependency_manifest", "Python", "setuptools", "project_dependencies"),
        "environment.yml": ("dependency_manifest", "Python/R", "conda", "environment_dependencies"),
        "environment.yaml": ("dependency_manifest", "Python/R", "conda", "environment_dependencies"),
        "pom.xml": ("dependency_manifest", "Java", "maven", "project_dependencies"),
        "build.gradle": ("dependency_manifest", "JVM", "gradle", "project_dependencies"),
        "build.gradle.kts": ("dependency_manifest", "JVM", "gradle", "project_dependencies"),
        "gradle.lockfile": ("lockfile", "JVM", "gradle", "resolved_dependencies"),
        "go.mod": ("dependency_manifest", "Go", "go-modules", "direct_dependencies"),
        "go.sum": ("lockfile", "Go", "go-modules", "resolved_dependencies"),
        "cargo.toml": ("dependency_manifest", "Rust", "cargo", "direct_dependencies"),
        "cargo.lock": ("lockfile", "Rust", "cargo", "resolved_dependencies"),
        "gemfile": ("dependency_manifest", "Ruby", "bundler", "direct_dependencies"),
        "gemfile.lock": ("lockfile", "Ruby", "bundler", "resolved_dependencies"),
        "composer.json": ("dependency_manifest", "PHP", "composer", "direct_dependencies"),
        "composer.lock": ("lockfile", "PHP", "composer", "resolved_dependencies"),
        "mix.exs": ("dependency_manifest", "Elixir", "mix", "project_dependencies"),
        "mix.lock": ("lockfile", "Elixir", "mix", "resolved_dependencies"),
        "pubspec.yaml": ("dependency_manifest", "Dart", "pub", "direct_dependencies"),
        "pubspec.lock": ("lockfile", "Dart", "pub", "resolved_dependencies"),
        "packages.config": ("dependency_manifest", ".NET", "nuget", "direct_dependencies"),
        "packages.lock.json": ("lockfile", ".NET", "nuget", "resolved_dependencies"),
        "paket.dependencies": ("dependency_manifest", ".NET", "paket", "direct_dependencies"),
        "paket.lock": ("lockfile", ".NET", "paket", "resolved_dependencies"),
        "renv.lock": ("lockfile", "R", "renv", "resolved_dependencies"),
        "DESCRIPTION": ("dependency_manifest", "R", "r-packaging", "project_dependencies"),
        "Package.swift": ("dependency_manifest", "Swift", "swift-package-manager", "direct_dependencies"),
        "Package.resolved": ("lockfile", "Swift", "swift-package-manager", "resolved_dependencies"),
        "Podfile": ("dependency_manifest", "Swift/Objective-C", "cocoapods", "direct_dependencies"),
        "Podfile.lock": ("lockfile", "Swift/Objective-C", "cocoapods", "resolved_dependencies"),
        "Cartfile": ("dependency_manifest", "Swift/Objective-C", "carthage", "direct_dependencies"),
        "Cartfile.resolved": ("lockfile", "Swift/Objective-C", "carthage", "resolved_dependencies"),
        "Dockerfile": ("container_build", "Container", "docker", "runtime_build"),
        "Containerfile": ("container_build", "Container", "podman", "runtime_build"),
        "docker-compose.yml": ("container_orchestration", "Container", "docker-compose", "runtime_orchestration"),
        "docker-compose.yaml": ("container_orchestration", "Container", "docker-compose", "runtime_orchestration"),
        "Jenkinsfile": ("ci_workflow", "CI/CD", "jenkins", "pipeline"),
        "Gemfile": ("dependency_manifest", "Ruby", "bundler", "direct_dependencies"),
        "Gemfile.lock": ("lockfile", "Ruby", "bundler", "resolved_dependencies"),
    }
    if name in exact:
        category, ecosystem, manager, role = exact[name]
        return {
            "is_supply_chain_file": "true",
            "supply_chain_match_type": category,
            "dependency_ecosystem": ecosystem,
            "package_manager": manager,
            "supply_chain_role": role,
        }
    if lower_name in exact:
        category, ecosystem, manager, role = exact[lower_name]
        return {
            "is_supply_chain_file": "true",
            "supply_chain_match_type": category,
            "dependency_ecosystem": ecosystem,
            "package_manager": manager,
            "supply_chain_role": role,
        }

    if lower_path.startswith(".github/workflows/") and lower_name.endswith((".yml", ".yaml")):
        return {
            "is_supply_chain_file": "true",
            "supply_chain_match_type": "ci_workflow",
            "dependency_ecosystem": "CI/CD",
            "package_manager": "github-actions",
            "supply_chain_role": "pipeline",
        }
    if lower_name.endswith(".csproj") or lower_name.endswith(".fsproj") or lower_name.endswith(".vbproj"):
        return {
            "is_supply_chain_file": "true",
            "supply_chain_match_type": "dependency_manifest",
            "dependency_ecosystem": ".NET",
            "package_manager": "nuget",
            "supply_chain_role": "project_dependencies",
        }
    if lower_name.endswith(".sln"):
        return {
            "is_supply_chain_file": "true",
            "supply_chain_match_type": "project_manifest",
            "dependency_ecosystem": ".NET",
            "package_manager": "dotnet",
            "supply_chain_role": "solution_structure",
        }
    if lower_name.endswith(".tf") or lower_name.endswith(".tfvars"):
        return {
            "is_supply_chain_file": "true",
            "supply_chain_match_type": "infrastructure_as_code",
            "dependency_ecosystem": "Infrastructure",
            "package_manager": "terraform",
            "supply_chain_role": "infrastructure_dependencies",
        }
    if lower_name in {"chart.yaml", "chart.lock"}:
        return {
            "is_supply_chain_file": "true",
            "supply_chain_match_type": "deployment_manifest",
            "dependency_ecosystem": "Kubernetes",
            "package_manager": "helm",
            "supply_chain_role": "deployment_dependencies",
        }
    if lower_path.endswith((".github/dependabot.yml", ".github/dependabot.yaml")):
        return {
            "is_supply_chain_file": "true",
            "supply_chain_match_type": "dependency_update_config",
            "dependency_ecosystem": "Multi-ecosystem",
            "package_manager": "dependabot",
            "supply_chain_role": "dependency_update_policy",
        }
    if lower_path.endswith((".github/codeql.yml", ".github/codeql.yaml")):
        return {
            "is_supply_chain_file": "true",
            "supply_chain_match_type": "security_scanning_config",
            "dependency_ecosystem": "Security",
            "package_manager": "codeql",
            "supply_chain_role": "security_analysis_policy",
        }

    return {
        "is_supply_chain_file": "false",
        "supply_chain_match_type": "",
        "dependency_ecosystem": "",
        "package_manager": "",
        "supply_chain_role": "",
    }


def summarize_commit_languages(files: list[dict[str, Any]]) -> tuple[list[str], str]:
    weights: Counter[str] = Counter()
    for file_row in files:
        language = file_row.get("file_language") or ""
        if not language:
            continue
        changes = int(file_row.get("changes") or 0)
        weights[language] += changes
    languages = sorted(weights)
    primary = weights.most_common(1)[0][0] if weights else ""
    return languages, primary


def summarize_supply_chain(files: list[dict[str, Any]]) -> dict[str, str]:
    matched = [row for row in files if row.get("is_supply_chain_file") == "true"]
    return {
        "touches_supply_chain": "true" if matched else "false",
        "supply_chain_file_count": str(len(matched)),
        "supply_chain_match_types": ";".join(sorted({row.get("supply_chain_match_type", "") for row in matched if row.get("supply_chain_match_type", "")})),
        "dependency_ecosystems": ";".join(sorted({row.get("dependency_ecosystem", "") for row in matched if row.get("dependency_ecosystem", "")})),
        "package_managers": ";".join(sorted({row.get("package_manager", "") for row in matched if row.get("package_manager", "")})),
    }


def summarize_verification(data: dict[str, Any]) -> dict[str, str]:
    verification = (data.get("commit") or {}).get("verification") or {}
    signature = verification.get("signature") or ""
    verification_type = ""
    if "BEGIN SSH SIGNATURE" in signature:
        verification_type = "ssh"
    elif "BEGIN PGP SIGNATURE" in signature:
        verification_type = "gpg"
    elif "BEGIN PKCS7" in signature or "BEGIN CMS" in signature:
        verification_type = "smime"
    elif signature:
        verification_type = "other"
    return {
        "verification_verified": str(verification.get("verified", "")).lower(),
        "verification_reason": verification.get("reason", "") or "",
        "verification_type": verification_type,
        "verification_verified_at": verification.get("verified_at", "") or "",
    }


def build_patch_text(data: dict[str, Any], files: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for file_row in files:
        filename = file_row.get("filename") or ""
        previous = file_row.get("previous_filename") or filename
        status = file_row.get("status") or ""
        patch = file_row.get("patch")
        old_path = "/dev/null" if status == "added" else f"a/{previous}"
        new_path = "/dev/null" if status == "removed" else f"b/{filename}"
        lines.append(f"diff --git a/{previous} b/{filename}")
        if status == "added":
            lines.append("new file mode 100644")
        elif status == "removed":
            lines.append("deleted file mode 100644")
        elif status == "renamed":
            lines.append(f"rename from {previous}")
            lines.append(f"rename to {filename}")
        lines.append(f"--- {old_path}")
        lines.append(f"+++ {new_path}")
        if patch:
            lines.append(str(patch).rstrip("\n"))
        else:
            lines.append("# patch unavailable: binary file or GitHub omitted a large patch")
        lines.append("")
    return "\n".join(lines)


def compact_commit_json(data: dict[str, Any]) -> dict[str, Any]:
    compact = dict(data)
    compact_files = []
    for file_row in data.get("files", []) or []:
        item = dict(file_row)
        item.pop("patch", None)
        compact_files.append(item)
    compact["files"] = compact_files
    return compact


def delta_commit_json(
    data: dict[str, Any],
    repo: str,
    sha: str,
    repo_language: str,
    commit_languages: list[str],
    commit_primary_language: str,
) -> dict[str, Any]:
    files = []
    for file_row in data.get("files", []) or []:
        files.append(
            {
                "sha": file_row.get("sha", ""),
                "filename": file_row.get("filename", ""),
                "previous_filename": file_row.get("previous_filename", ""),
                "status": file_row.get("status", ""),
                "additions": file_row.get("additions", ""),
                "deletions": file_row.get("deletions", ""),
                "changes": file_row.get("changes", ""),
                "blob_url": file_row.get("blob_url", ""),
                "raw_url": file_row.get("raw_url", ""),
                "contents_url": file_row.get("contents_url", ""),
                "file_language": file_row.get("file_language", ""),
                "file_language_source": file_row.get("file_language_source", ""),
                "is_supply_chain_file": file_row.get("is_supply_chain_file", "false"),
                "supply_chain_match_type": file_row.get("supply_chain_match_type", ""),
                "dependency_ecosystem": file_row.get("dependency_ecosystem", ""),
                "package_manager": file_row.get("package_manager", ""),
                "supply_chain_role": file_row.get("supply_chain_role", ""),
                "has_patch": bool(file_row.get("patch")),
            }
        )
    return {
        "schema": "get_commit_delta_v1",
        "repo": repo,
        "sha": sha,
        "repo_sha": f"{repo}|{sha}",
        "repo_language": repo_language or "",
        "commit_languages": commit_languages,
        "commit_primary_language": commit_primary_language,
        **summarize_supply_chain(data.get("files", []) or []),
        "html_url": data.get("html_url", ""),
        "stats": data.get("stats") or {},
        "verification": summarize_verification(data),
        "files": files,
    }


def parse_link_header(value: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for part in value.split(","):
        sections = part.strip().split(";")
        if len(sections) < 2:
            continue
        url = sections[0].strip()
        if not (url.startswith("<") and url.endswith(">")):
            continue
        rel = ""
        for section in sections[1:]:
            section = section.strip()
            if section.startswith('rel="') and section.endswith('"'):
                rel = section[5:-1]
        if rel:
            links[rel] = url[1:-1]
    return links


def rate_sleep(headers: Any, floor: int, buffer_seconds: int) -> None:
    try:
        remaining = int(headers.get("X-RateLimit-Remaining", "-1"))
        reset_epoch = int(headers.get("X-RateLimit-Reset", "0"))
    except ValueError:
        return
    if remaining < 0 or remaining > floor or reset_epoch <= 0:
        return
    sleep_seconds = max(0, reset_epoch - int(time.time()) + buffer_seconds)
    if sleep_seconds:
        print(f"rate_limit_wait remaining={remaining} sleep_seconds={sleep_seconds}")
        time.sleep(sleep_seconds)


def request_json(
    url: str,
    token: str,
    max_retries: int,
    retry_base_seconds: int,
) -> tuple[dict[str, Any], Any, int, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "commit-json-diff-language-fetcher",
        "Authorization": f"token {token}",
    }
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                body = response.read().decode("utf-8")
                return json.loads(body), response.headers, response.status, ""
        except urllib.error.HTTPError as error:
            status = int(error.code)
            message = read_http_error_message(error)
            if status == 401 and attempt < max_retries:
                delay = min(10, 2 * attempt)
                print(
                    f"request_unauthorized_retry attempt={attempt} "
                    f"sleep_seconds={delay} url={url}"
                )
                time.sleep(delay)
                continue
            if is_retryable_limit_error(status, error.headers, message) and attempt < max_retries:
                delay = retry_base_seconds * attempt
                reset = error.headers.get("X-RateLimit-Reset")
                if reset:
                    try:
                        delay = max(delay, int(reset) - int(time.time()) + 30)
                    except ValueError:
                        pass
                print(f"request_limited status={status} sleep_seconds={delay}")
                time.sleep(delay)
                continue
            return {}, error.headers, status, message
        except Exception as error:
            if attempt >= max_retries:
                return {}, {}, 0, f"{type(error).__name__}: {error}"
            time.sleep(retry_base_seconds * attempt)
    return {}, {}, 0, "max retries exhausted"


def read_http_error_message(error: urllib.error.HTTPError) -> str:
    try:
        body = error.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    if not body:
        return str(error)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body
    message = data.get("message")
    if message:
        return json.dumps(data, ensure_ascii=False)
    return body


def is_retryable_limit_error(status: int, headers: Any, message: str) -> bool:
    if status == 429:
        return True
    if status != 403:
        return False
    lowered = message.lower()
    if "rate limit" in lowered or "secondary rate limit" in lowered or "abuse detection" in lowered:
        return True
    try:
        remaining = int(headers.get("X-RateLimit-Remaining", "-1"))
    except ValueError:
        remaining = -1
    if remaining == 0:
        return True
    if headers.get("Retry-After"):
        return True
    return False


def fetch_commit(
    repo: str,
    sha: str,
    token: str,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[dict[str, Any]], int, str]:
    base = f"https://api.github.com/repos/{repo}/commits/{sha}"
    params = urllib.parse.urlencode({"per_page": "100", "page": "1"})
    next_url = f"{base}?{params}"
    first_data: dict[str, Any] | None = None
    files: list[dict[str, Any]] = []
    seen_files: set[tuple[str, str, str]] = set()
    last_status = 0

    while next_url:
        data, headers, status, error = request_json(
            next_url, token, args.max_retries, args.retry_base_seconds
        )
        last_status = status
        if error:
            return {}, [], status, error
        if first_data is None:
            first_data = data
        for file_row in data.get("files", []) or []:
            key = (
                file_row.get("filename", ""),
                file_row.get("previous_filename", ""),
                file_row.get("status", ""),
            )
            if key in seen_files:
                continue
            seen_files.add(key)
            language, source = detect_file_language(file_row.get("filename", ""))
            supply_chain = classify_supply_chain_file(file_row.get("filename", ""))
            file_row = dict(file_row)
            file_row["file_language"] = language
            file_row["file_language_source"] = source
            file_row.update(supply_chain)
            files.append(file_row)
        rate_sleep(headers, args.rate_limit_floor, args.rate_limit_buffer_seconds)
        links = parse_link_header(headers.get("Link", ""))
        next_url = links.get("next", "")

    merged = dict(first_data or {})
    merged["files"] = files
    return merged, files, last_status, ""


def reload_and_verify_token(
    current_token: str, args: argparse.Namespace,
) -> tuple[str, int, str]:
    refreshed = load_token() or current_token
    data, _headers, status, error = request_json(
        "https://api.github.com/user",
        refreshed,
        args.max_retries,
        min(args.retry_base_seconds, 5),
    )
    if status == 200:
        source = "reloaded" if refreshed != current_token else "unchanged"
        print(
            f"github_token_verified status=200 source={source} "
            f"login={data.get('login', '')}"
        )
    return refreshed, status, error


def recover_unauthorized_commit(
    repo: str, sha: str, token: str, args: argparse.Namespace,
) -> tuple[dict[str, Any], list[dict[str, Any]], int, str, str, int]:
    refreshed, validation_status, validation_error = reload_and_verify_token(token, args)
    if validation_status != 200:
        return {}, [], 401, validation_error, refreshed, validation_status
    print(f"request_unauthorized_token_valid retry_commit repo={repo} sha={sha}")
    data, files, status, error = fetch_commit(repo, sha, refreshed, args)
    if status == 401 and error:
        error = f"auth_401_after_verified_token: {error}"
    return data, files, status, error, refreshed, validation_status


def output_paths(output_root: Path, language_bucket: str, repo: str, sha: str) -> tuple[Path, Path]:
    language_root = output_root / safe_segment(language_bucket or "_unknown")
    repo_root = language_root / safe_segment(repo.replace("/", "__"))
    return repo_root / f"{sha}.json", repo_root / f"{sha}.patch"


def commit_rows(args: argparse.Namespace) -> Any:
    language_filter = {value.lower() for value in args.languages} if args.languages else None
    fallback = read_repo_language_fallback(args.repo_metadata_csv)
    seen: set[str] = set()
    with args.commit_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            repo = (row.get("repo") or "").strip()
            sha = (row.get("sha") or "").strip()
            if not repo or not sha:
                continue
            key = sha if args.dedupe_key == "sha" else f"{repo}@{sha}"
            if key in seen:
                continue
            seen.add(key)
            repo_language = (row.get("repo_language") or fallback.get(repo) or "").strip()
            if language_filter is not None and repo_language.lower() not in language_filter:
                continue
            yield row, repo, sha, repo_language


def main() -> None:
    args = parse_args()
    if args.no_save_json:
        args.json_mode = "none"
    token = load_token()
    if not token:
        raise SystemExit("ERROR: GITHUB_TOKEN not found in scripts/.env or environment")

    args.output_root.mkdir(parents=True, exist_ok=True)
    index_path = args.output_root / "commit_index.csv"
    files_path = args.output_root / "changed_files.csv"
    manifest_dir = args.output_root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.force:
        for csv_path in (index_path, files_path):
            if csv_path.exists():
                csv_path.unlink()

    index_fields = [
        "repo_language",
        "commit_primary_language",
        "commit_languages",
        "touches_supply_chain",
        "supply_chain_file_count",
        "supply_chain_match_types",
        "dependency_ecosystems",
        "package_managers",
        "verification_verified",
        "verification_reason",
        "verification_type",
        "repo",
        "sha",
        "status",
        "http_status",
        "files_count",
        "additions",
        "deletions",
        "total_changes",
        "patch_size_bytes",
        "json_path",
        "patch_path",
        "html_url",
        "error",
        "fetched_utc",
    ]
    file_fields = [
        "repo_language",
        "commit_primary_language",
        "repo",
        "sha",
        "filename",
        "previous_filename",
        "status",
        "file_language",
        "file_language_source",
        "is_supply_chain_file",
        "supply_chain_match_type",
        "dependency_ecosystem",
        "package_manager",
        "supply_chain_role",
        "additions",
        "deletions",
        "changes",
        "has_patch",
        "raw_url",
        "blob_url",
    ]

    completed_keys: set[str] = set()
    error_skip_keys: set[str] = set()
    permanent_statuses = parse_status_set(args.permanent_error_statuses)
    cooldown_since = datetime.now(timezone.utc) - timedelta(hours=args.error_cooldown_hours)
    if index_path.exists() and not args.force:
        for existing in iter_existing_index_rows(index_path):
            key = existing.get("sha", "") if args.dedupe_key == "sha" else f"{existing.get('repo', '')}@{existing.get('sha', '')}"
            if not key:
                continue
            if existing.get("status") == "ok":
                patch_ok = bool(existing.get("patch_path")) and Path(existing["patch_path"]).exists()
                json_ok = args.json_mode == "none" or (
                    bool(existing.get("json_path")) and Path(existing["json_path"]).exists()
                )
                if patch_ok and json_ok:
                    completed_keys.add(key)
                continue
            try:
                http_status = int(existing.get("http_status") or 0)
            except ValueError:
                http_status = 0
            fetched_at = parse_utc(existing.get("fetched_utc", ""))
            recent_error = bool(fetched_at and fetched_at >= cooldown_since)
            permanent_error = http_status in permanent_statuses
            if recent_error or permanent_error:
                error_skip_keys.add(key)

    print(
        "==== startup "
        f"completed_ok={len(completed_keys)} skipped_error_keys={len(error_skip_keys)} "
        f"progress_interval={args.progress_interval} "
        f"error_cooldown_hours={args.error_cooldown_hours} "
        f"permanent_error_statuses={sorted(permanent_statuses)} ====",
        flush=True,
    )

    fetched = skipped = error_skipped = errors = matched = 0
    started_at = time.time()
    for row, repo, sha, repo_language in commit_rows(args):
        if args.max_commits > 0 and fetched + skipped + error_skipped + errors >= args.max_commits:
            break
        matched += 1
        dedupe_value = sha if args.dedupe_key == "sha" else f"{repo}@{sha}"
        if dedupe_value in completed_keys:
            skipped += 1
            if args.log_skipped:
                append_csv(
                    index_path,
                    [
                        {
                            "repo_language": repo_language or "<blank>",
                            "repo": repo,
                            "sha": sha,
                            "status": "skipped_existing",
                            "html_url": row.get("html_url", ""),
                            "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        }
                    ],
                    index_fields,
                )
            if args.progress_interval > 0 and (fetched + skipped + error_skipped + errors) % args.progress_interval == 0:
                print_progress(fetched, skipped, error_skipped, errors, matched, started_at)
            continue

        if dedupe_value in error_skip_keys:
            error_skipped += 1
            if args.progress_interval > 0 and (fetched + skipped + error_skipped + errors) % args.progress_interval == 0:
                print_progress(fetched, skipped, error_skipped, errors, matched, started_at)
            continue

        print(f"fetch_commit repo_language={repo_language or '<blank>'} repo={repo} sha={sha}")
        data, files, http_status, error = fetch_commit(repo, sha, token, args)
        auth_validation_status = 0
        if error and http_status == 401:
            (
                data,
                files,
                http_status,
                error,
                token,
                auth_validation_status,
            ) = recover_unauthorized_commit(repo, sha, token, args)
            if auth_validation_status == 401:
                raise SystemExit(
                    "ERROR: GitHub token and /user validation both returned 401; "
                    "stop before writing more error rows."
                )
            if auth_validation_status != 200:
                raise SystemExit(
                    "ERROR: GitHub token could not be verified after commit 401 "
                    f"(validation_status={auth_validation_status}); stop before writing more error rows."
                )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if error:
            errors += 1
            append_csv(
                index_path,
                [
                    {
                        "repo_language": repo_language or "<blank>",
                        "repo": repo,
                        "sha": sha,
                        "status": "error",
                        "http_status": http_status,
                        "html_url": row.get("html_url", ""),
                        "error": error,
                        "fetched_utc": now,
                    }
                ],
                index_fields,
            )
            if args.progress_interval > 0 and (fetched + skipped + error_skipped + errors) % args.progress_interval == 0:
                print_progress(fetched, skipped, error_skipped, errors, matched, started_at)
            time.sleep(args.sleep_seconds)
            continue

        patch_text = build_patch_text(data, files)
        languages, primary_language = summarize_commit_languages(files)
        supply_chain_summary = summarize_supply_chain(files)
        verification_summary = summarize_verification(data)
        json_path, patch_path = output_paths(args.output_root, primary_language, repo, sha)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with patch_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(patch_text)
        if args.json_mode != "none":
            if args.json_mode == "full":
                json_data = data
            elif args.json_mode == "compact":
                json_data = compact_commit_json(data)
            else:
                json_data = delta_commit_json(
                    data, repo, sha, repo_language, languages, primary_language
                )
            json_path.write_text(json.dumps(json_data, ensure_ascii=False), encoding="utf-8")

        stats = data.get("stats") or {}
        append_csv(
            index_path,
            [
                {
                    "repo_language": repo_language or "<blank>",
                    "commit_primary_language": primary_language,
                    "commit_languages": ";".join(languages),
                    **supply_chain_summary,
                    **verification_summary,
                    "repo": repo,
                    "sha": sha,
                    "status": "ok",
                    "http_status": http_status,
                    "files_count": len(files),
                    "additions": stats.get("additions", ""),
                    "deletions": stats.get("deletions", ""),
                    "total_changes": stats.get("total", ""),
                    "patch_size_bytes": len(patch_text.encode("utf-8")),
                    "json_path": "" if args.json_mode == "none" else str(json_path),
                    "patch_path": str(patch_path),
                    "html_url": data.get("html_url") or row.get("html_url", ""),
                    "error": "",
                    "fetched_utc": now,
                }
            ],
            index_fields,
        )
        file_rows = []
        for file_row in files:
            file_rows.append(
                {
                    "repo_language": repo_language or "<blank>",
                    "commit_primary_language": primary_language,
                    "repo": repo,
                    "sha": sha,
                    "filename": file_row.get("filename", ""),
                    "previous_filename": file_row.get("previous_filename", ""),
                    "status": file_row.get("status", ""),
                    "file_language": file_row.get("file_language", ""),
                    "file_language_source": file_row.get("file_language_source", ""),
                    "is_supply_chain_file": file_row.get("is_supply_chain_file", "false"),
                    "supply_chain_match_type": file_row.get("supply_chain_match_type", ""),
                    "dependency_ecosystem": file_row.get("dependency_ecosystem", ""),
                    "package_manager": file_row.get("package_manager", ""),
                    "supply_chain_role": file_row.get("supply_chain_role", ""),
                    "additions": file_row.get("additions", ""),
                    "deletions": file_row.get("deletions", ""),
                    "changes": file_row.get("changes", ""),
                    "has_patch": bool(file_row.get("patch")),
                    "raw_url": file_row.get("raw_url", ""),
                    "blob_url": file_row.get("blob_url", ""),
                }
            )
        append_csv(files_path, file_rows, file_fields)
        fetched += 1
        if args.progress_interval > 0 and (fetched + skipped + error_skipped + errors) % args.progress_interval == 0:
            print_progress(fetched, skipped, error_skipped, errors, matched, started_at)
        time.sleep(args.sleep_seconds)

    manifest = {
        "commit_csv": str(args.commit_csv),
        "repo_metadata_csv": str(args.repo_metadata_csv),
        "output_root": str(args.output_root),
        "languages": args.languages,
        "max_commits": args.max_commits,
        "json_mode": args.json_mode,
        "dedupe_key": args.dedupe_key,
        "fetched": fetched,
        "skipped": skipped,
        "error_skipped": error_skipped,
        "errors": errors,
        "matched": matched,
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    manifest_path = manifest_dir / f"commit_json_diff_fetch_{run_stamp}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done fetched={fetched} skipped={skipped} error_skipped={error_skipped} errors={errors} matched={matched}")
    print(f"index={index_path}")
    print(f"changed_files={files_path}")
    print(f"manifest={manifest_path}")


if __name__ == "__main__":
    main()

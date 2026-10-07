"""Extract import events and candidate commits from the local commit-diff corpus.

[IN]: commit_index.csv, merged agent population CSV, and local commit patch/JSON files.
[OUT]: candidate_commits.csv, import_events.csv, dependency_resolution.csv,
sampling_audit.csv, rq2_hallucinated_pilot_report.md, and stage1_checkpoint.json.
[POS]: Local, no-network Stage1 import extraction for the hallucinated-dependency pipeline.
[SYNC]: If import rules, sampling, checkpoint fields, or output schemas change,
update scripts/OUTPUTS.md and .claude/plans/2026-07-07-hallucination-full-pipeline.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass
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
from typing import Iterable

DEFAULT_INDEX = Path(
    r"D:\MasterThesis\thesis-work-data\diff_corpus\commit_json_diffs_by_language\commit_index.csv"
)
DEFAULT_POPULATION = Path("data_products/agent_commit_population_merged_1y_v1/merged_agent_commit_population.csv")
DEFAULT_OUTPUT = Path("data_products/rq2_hallucinated_pilot")
DEFAULT_LANGUAGES = ["TypeScript", "Python", "JavaScript", "C#", "Rust", "Go", "PHP", "Java", "C++", "Kotlin"]
DEFAULT_EXCLUDE_LANGUAGES = {
    "",
    "<blank>",
    "HTML",
    "Shell",
    "Markdown",
    "MDX",
    "CSS",
    "SCSS",
    "Sass",
    "Less",
    "JSON",
    "JSON with Comments",
    "JSON_with_Comments",
    "YAML",
    "XML",
    "TOML",
    "TeX",
    "BibTeX",
    "Dockerfile",
    "Makefile",
    "CMake",
    "HCL",
    "SQL",
    "PLpgSQL",
    "Protocol Buffer",
    "Protocol_Buffer",
    "Batchfile",
    "PowerShell",
    "Procfile",
    "Jupyter Notebook",
    "unknown",
}

ADDED_LINE = re.compile(r"^\+(?!\+\+)(.*)$")
DIFF_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$")
NEW_FILE_HEADER = re.compile(r"^\+\+\+ b/(.*)$")

JS_IMPORTS = [
    re.compile(r"^\s*import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]"),
    re.compile(r"^\s*import\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"^\s*(?:const|let|var)\s+.*?=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"\bimport\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)"),
]
PY_IMPORTS = [
    re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)"),
    re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\b"),
]
RUST_IMPORTS = [re.compile(r"^\s*use\s+([A-Za-z_][\w]*)")]
JVM_IMPORTS = [re.compile(r"^\s*import\s+(?:static\s+)?([A-Za-z_][\w.]*)\s*;?")]
GO_IMPORTS = [
    re.compile(r'^\s*import\s+(?:[._\w]+\s+)?["`]([^"`]+)["`]'),
]
RUBY_IMPORTS = [
    re.compile(r"^\s*require\s+['\"]([^'\"]+)['\"]"),
    re.compile(r"^\s*gem\s+['\"]([^'\"]+)['\"]"),
]
PHP_IMPORTS = [
    re.compile(r"^\s*use\s+([A-Za-z_\\][\w\\]+)\s*;"),
    re.compile(r"^\s*(?:require|include)(?:_once)?\s+['\"]([^'\"]+)['\"]"),
]
DART_IMPORTS = [re.compile(r"^\s*import\s+['\"]([^'\"]+)['\"]")]
SWIFT_IMPORTS = [re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)")]
CSHARP_IMPORTS = [re.compile(r"^\s*using\s+([A-Za-z_][\w.]*)\s*;")]
FSHARP_IMPORTS = [re.compile(r"^\s*open\s+([A-Za-z_][\w.]*)")]
R_IMPORTS = [re.compile(r"^\s*(?:library|require)\(\s*['\"]?([A-Za-z0-9_.-]+)['\"]?\s*\)")]
LUA_IMPORTS = [re.compile(r"^\s*(?:local\s+\w+\s*=\s*)?require\s*(?:\(?\s*)['\"]([^'\"]+)['\"]")]
JULIA_IMPORTS = [re.compile(r"^\s*(?:using|import)\s+([A-Za-z_][\w.]*)")]
HASKELL_IMPORTS = [re.compile(r"^\s*import\s+(?:qualified\s+)?([A-Za-z_][\w.']*)")]
ELIXIR_IMPORTS = [re.compile(r"^\s*(?:alias|import|require|use)\s+([A-Z][\w.]+)")]
SOLIDITY_IMPORTS = [
    re.compile(r"^\s*import\s+['\"]([^'\"]+)['\"]"),
    re.compile(r"^\s*import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]"),
]
C_INCLUDE_IMPORTS = [re.compile(r"^\s*#\s*include\s+[<\"]([^>\"]+)[>\"]")]
ZIG_IMPORTS = [re.compile(r"@import\(\s*['\"]([^'\"]+)['\"]\s*\)")]
LEAN_IMPORTS = [re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)")]

NODE_BUILTINS = {
    "assert", "buffer", "child_process", "cluster", "console", "crypto", "dns", "events", "fs",
    "http", "https", "module", "net", "os", "path", "process", "querystring", "stream",
    "string_decoder", "timers", "tls", "tty", "url", "util", "vm", "zlib",
}
PY_STDLIB_HINTS = {
                      "argparse", "asyncio", "collections", "csv", "dataclasses", "datetime", "functools", "gc",
                      "copy", "gzip", "html", "http", "inspect", "io", "itertools", "json", "logging",
                      "math", "os", "pathlib", "platform", "random", "re", "shutil", "sqlite3",
                      "subprocess", "sys", "tempfile", "threading", "time", "typing", "unittest",
                      "urllib", "uuid",
                  } | set(getattr(sys, "stdlib_module_names", ()))
PY_ALIAS = {"bs4": "beautifulsoup4", "cv2": "opencv-python", "dotenv": "python-dotenv", "PIL": "pillow",
            "sklearn": "scikit-learn", "yaml": "pyyaml"}
JAVA_STDLIB_PREFIXES = ("java.", "javax.")
CSHARP_STDLIB_PREFIXES = ("System",)
GO_STDLIB_ROOTS = {
    "archive", "bufio", "bytes", "compress", "context", "crypto", "database", "embed", "encoding",
    "errors", "expvar", "flag", "fmt", "hash", "html", "image", "index", "io", "log", "math",
    "mime", "net", "os", "path", "reflect", "regexp", "runtime", "sort", "strconv", "strings",
    "sync", "testing", "text", "time", "unicode",
}
KOTLIN_STDLIB_PREFIXES = ("kotlin.", "kotlinx.")
CPP_STDLIB_HEADERS = {
    "algorithm", "array", "atomic", "bit", "cassert", "cctype", "cerrno", "cfenv", "cfloat", "charconv",
    "chrono", "climits", "cmath", "codecvt", "compare", "complex", "concepts", "condition_variable",
    "coroutine", "csetjmp", "csignal", "cstdarg", "cstddef", "cstdint", "cstdio", "cstdlib", "cstring",
    "ctime", "deque", "exception", "execution", "filesystem", "format", "fstream", "functional", "future",
    "initializer_list", "iomanip", "ios", "iosfwd", "iostream", "istream", "iterator", "limits", "list",
    "locale", "map", "memory", "mutex", "new", "numeric", "optional", "ostream", "queue", "random",
    "ranges", "regex", "set", "shared_mutex", "span", "sstream", "stack", "stdexcept", "streambuf",
    "string", "string_view", "strstream", "syncstream", "system_error", "thread", "tuple", "type_traits",
    "typeindex", "typeinfo", "unordered_map", "unordered_set", "utility", "valarray", "variant", "vector",
    "assert.h", "ctype.h", "errno.h", "float.h", "limits.h", "locale.h", "math.h", "setjmp.h", "signal.h",
    "stdarg.h", "stddef.h", "stdint.h", "stdio.h", "stdlib.h", "string.h", "time.h", "wchar.h", "wctype.h",
}


@dataclass
class ImportEvent:
    repo: str
    sha: str
    author_date: str
    agent: str
    repo_language: str
    ecosystem: str
    source_file: str
    import_raw: str
    import_root: str
    package_candidate: str
    import_kind: str
    mapping_status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit-index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--population-csv", type=Path, default=DEFAULT_POPULATION)
    parser.add_argument("--languages", nargs="*", default=DEFAULT_LANGUAGES)
    parser.add_argument("--all-languages", action="store_true",
                        help="Try every repo_language except --exclude-languages.")
    parser.add_argument("--exclude-languages", nargs="*", default=sorted(DEFAULT_EXCLUDE_LANGUAGES))
    parser.add_argument("--sample-per-language", type=int, default=100,
                        help="Maximum external-candidate commits per language; 0 means unlimited.")
    parser.add_argument("--max-total-candidates", type=int, default=0)
    parser.add_argument("--sampling-mode", choices=["random", "first"], default="random")
    parser.add_argument("--random-seed", type=int, default=20260701)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-index-rows", type=int, default=0)
    parser.add_argument("--checkpoint-every-candidates", type=int, default=0,
                        help="Write Stage1 CSV outputs every N selected candidates; 0 disables checkpoint writes.")
    parser.add_argument("--resume-stage1", dest="resume_stage1", action="store_true", default=True,
                        help="Resume incomplete Stage1 first-mode checkpoints from output-dir when possible.")
    parser.add_argument("--no-resume-stage1", dest="resume_stage1", action="store_false",
                        help="Ignore incomplete Stage1 checkpoints and rebuild Stage1 from scratch.")
    parser.add_argument("--no-network", action="store_true", default=True)
    return parser.parse_args()


def clean_lines(path: Path) -> Iterable[str]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for line in handle:
            yield line.replace("\x00", "")


def load_population(population_csv: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(clean_lines(population_csv)):
        key = row.get("repo_sha") or f"{row.get('repo', '')}|{row.get('sha', '')}"
        if key and key not in result:
            result[key] = {"agent": row.get("agent", ""), "author_date": row.get("author_date", "")}
    return result


def iter_index_rows(index_path: Path, max_rows: int) -> Iterable[dict[str, str]]:
    count = 0
    for row in csv.DictReader(clean_lines(index_path)):
        if row.get("status") != "ok":
            continue
        count += 1
        row["_stage1_index_position"] = str(count)
        yield row
        if max_rows and count >= max_rows:
            break


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return list(csv.DictReader(clean_lines(path)))


def language_to_ecosystem(language: str) -> str:
    if language in {"JavaScript", "TypeScript", "TSX", "Vue", "Svelte", "Astro"}:
        return "npm"
    if language == "Python":
        return "PyPI"
    if language == "Rust":
        return "Cargo"
    if language in {"Java", "Kotlin", "Scala", "Groovy", "Clojure"}:
        return "Maven"
    if language == "Go":
        return "Go"
    if language == "Ruby":
        return "RubyGems"
    if language == "PHP":
        return "Packagist"
    if language == "Dart":
        return "Pub"
    if language == "Swift":
        return "SwiftPM"
    if language in {"C#", "F#"}:
        return "NuGet"
    if language == "R":
        return "CRAN"
    if language == "Lua":
        return "LuaRocks"
    if language in {"Elixir", "Erlang"}:
        return "Hex"
    if language == "Haskell":
        return "Hackage"
    if language == "Julia":
        return "JuliaGeneral"
    if language == "Solidity":
        return "npm_or_solidity"
    if language in {"C", "C++", "C_C++_Header", "Objective-C"}:
        return "system_or_native"
    return "unknown"


def extract_patch_evidence_from_path(
    row: dict[str, str], meta: dict[str, str],
) -> tuple[list[ImportEvent], list[str]]:
    patch_path = Path(row.get("patch_path", ""))
    if not patch_path.exists():
        return [], []
    try:
        with patch_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            return extract_imports_and_files(row, meta, handle)
    except OSError:
        return [], []


def extract_imports_from_patch(row: dict[str, str], meta: dict[str, str]) -> list[ImportEvent]:
    events, _files = extract_patch_evidence_from_path(row, meta)
    return events


def extract_imports(row: dict[str, str], meta: dict[str, str], patch: str) -> list[ImportEvent]:
    events, _files = extract_imports_and_files(row, meta, patch.splitlines())
    return events


def extract_imports_and_files(
    row: dict[str, str], meta: dict[str, str], lines: Iterable[str],
) -> tuple[list[ImportEvent], list[str]]:
    current_file = ""
    events: list[ImportEvent] = []
    changed_files: list[str] = []
    seen_files: set[str] = set()
    for raw_line in lines:
        raw = raw_line.rstrip("\r\n")
        current_file = update_current_file(raw, current_file)
        if current_file and current_file not in seen_files:
            changed_files.append(current_file)
            seen_files.add(current_file)
        match = ADDED_LINE.match(raw)
        if not match:
            continue
        event = import_event_from_line(row, meta, current_file, match.group(1))
        if event:
            events.append(event)
    return events, changed_files


def update_current_file(raw: str, current_file: str) -> str:
    diff_match = DIFF_HEADER.match(raw)
    if diff_match:
        return diff_match.group(2)
    new_match = NEW_FILE_HEADER.match(raw)
    if new_match:
        return new_match.group(1)
    return current_file


def import_event_from_line(row: dict[str, str], meta: dict[str, str], source_file: str,
                           line: str) -> ImportEvent | None:
    language = row.get("repo_language") or row.get("commit_primary_language", "")
    if not source_file_matches_language(source_file, language):
        return None
    raw_import = match_import(line, language)
    if not raw_import:
        return None
    root, package, status = normalize_import(raw_import, language)
    kind = status if status in {"relative_or_local", "stdlib_or_builtin"} else "external_candidate"
    return ImportEvent(
        repo=row.get("repo", ""),
        sha=row.get("sha", ""),
        author_date=meta.get("author_date", ""),
        agent=meta.get("agent", ""),
        repo_language=language,
        ecosystem=language_to_ecosystem(language),
        source_file=source_file,
        import_raw=raw_import,
        import_root=root,
        package_candidate=package,
        import_kind=kind,
        mapping_status=status,
    )


def source_file_matches_language(source_file: str, language: str) -> bool:
    path = source_file.lower().split("?", 1)[0]
    suffix = Path(path).suffix
    name = Path(path).name
    if language in {"JavaScript", "TypeScript", "TSX"}:
        return suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
    if language in {"Vue", "Svelte", "Astro"}:
        return suffix == f".{language.lower()}"
    if language == "Python":
        return suffix in {".py", ".pyw"}
    if language == "Rust":
        return suffix == ".rs"
    if language == "Go":
        return suffix == ".go"
    if language == "Java":
        return suffix == ".java"
    if language == "Kotlin":
        return suffix in {".kt", ".kts"}
    if language == "Scala":
        return suffix == ".scala"
    if language == "Groovy":
        return suffix == ".groovy"
    if language == "Ruby":
        return suffix == ".rb" or name in {"gemfile", "rakefile"}
    if language == "PHP":
        return suffix == ".php"
    if language == "Dart":
        return suffix == ".dart"
    if language == "Swift":
        return suffix == ".swift"
    if language == "C#":
        return suffix == ".cs"
    if language == "F#":
        return suffix in {".fs", ".fsx"}
    if language == "R":
        return suffix in {".r", ".R".lower()}
    if language == "Lua":
        return suffix == ".lua"
    if language == "Julia":
        return suffix == ".jl"
    if language == "Haskell":
        return suffix in {".hs", ".lhs"}
    if language in {"Elixir", "Erlang"}:
        return suffix in {".ex", ".exs", ".erl", ".hrl"}
    if language == "Solidity":
        return suffix == ".sol"
    if language in {"C", "C++", "C_C++_Header", "Objective-C"}:
        return suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".m", ".mm"}
    if language == "Zig":
        return suffix == ".zig"
    if language == "Lean":
        return suffix == ".lean"
    return True


def match_import(line: str, language: str) -> str:
    patterns = patterns_for_language(language)
    for pattern in patterns:
        match = pattern.search(line)
        if match:
            return match.group(1).strip()
    return ""


def patterns_for_language(language: str) -> list[re.Pattern[str]]:
    if language in {"JavaScript", "TypeScript", "TSX", "Vue", "Svelte", "Astro"}:
        return JS_IMPORTS
    if language == "Python":
        return PY_IMPORTS
    if language == "Rust":
        return RUST_IMPORTS
    if language in {"Java", "Kotlin", "Scala", "Groovy"}:
        return JVM_IMPORTS
    if language == "Go":
        return GO_IMPORTS
    if language == "Ruby":
        return RUBY_IMPORTS
    if language == "PHP":
        return PHP_IMPORTS
    if language == "Dart":
        return DART_IMPORTS
    if language == "Swift":
        return SWIFT_IMPORTS
    if language == "C#":
        return CSHARP_IMPORTS
    if language == "F#":
        return FSHARP_IMPORTS
    if language == "R":
        return R_IMPORTS
    if language == "Lua":
        return LUA_IMPORTS
    if language == "Julia":
        return JULIA_IMPORTS
    if language == "Haskell":
        return HASKELL_IMPORTS
    if language in {"Elixir", "Erlang"}:
        return ELIXIR_IMPORTS
    if language == "Solidity":
        return SOLIDITY_IMPORTS
    if language in {"C", "C++", "C_C++_Header", "Objective-C"}:
        return C_INCLUDE_IMPORTS
    if language == "Zig":
        return ZIG_IMPORTS
    if language == "Lean":
        return LEAN_IMPORTS
    return []


def normalize_import(raw_import: str, language: str) -> tuple[str, str, str]:
    if language in {"JavaScript", "TypeScript", "TSX", "Vue", "Svelte", "Astro"}:
        return normalize_js_import(raw_import)
    if language == "Python":
        return normalize_python_import(raw_import)
    if language == "Rust":
        return normalize_rust_import(raw_import)
    if language in {"Java", "Kotlin", "Scala", "Groovy"}:
        return normalize_jvm_import(raw_import)
    if language == "Go":
        return normalize_go_import(raw_import)
    if language == "Ruby":
        return normalize_path_like(raw_import, "direct_or_normalized_mapping")
    if language == "PHP":
        return normalize_namespace_unknown(raw_import, "php_namespace_mapping_unknown")
    if language == "Dart":
        return normalize_dart_import(raw_import)
    if language == "Swift":
        return normalize_module_unknown(raw_import, "swift_module_mapping_unknown")
    if language in {"C#", "F#"}:
        return normalize_dotnet_import(raw_import)
    if language in {"R", "Lua", "Julia"}:
        return normalize_module_direct(raw_import)
    if language == "Haskell":
        return normalize_module_unknown(raw_import, "haskell_module_mapping_unknown")
    if language in {"Elixir", "Erlang"}:
        return normalize_module_unknown(raw_import, "beam_module_mapping_unknown")
    if language == "Solidity":
        return normalize_solidity_import(raw_import)
    if language in {"C", "C++", "C_C++_Header", "Objective-C"}:
        return normalize_include_import(raw_import)
    if language == "Zig":
        return normalize_zig_import(raw_import)
    if language == "Lean":
        return normalize_module_unknown(raw_import, "lean_module_mapping_unknown")
    return raw_import, raw_import, "import_to_package_mapping_unknown"


def normalize_js_import(raw_import: str) -> tuple[str, str, str]:
    if raw_import.startswith((".", "/", "#", "@/", "~/")):
        return raw_import, "", "relative_or_local"
    value = raw_import.removeprefix("node:")
    parts = value.split("/")
    if parts[0] in NODE_BUILTINS:
        return parts[0], "", "stdlib_or_builtin"
    package = "/".join(parts[:2]) if value.startswith("@") and len(parts) > 1 else parts[0]
    return package, package, "direct_mapping"


def normalize_python_import(raw_import: str) -> tuple[str, str, str]:
    root = raw_import.split(".")[0]
    if root in PY_STDLIB_HINTS or root.startswith("_"):
        return root, "", "stdlib_or_builtin"
    package = PY_ALIAS.get(root, root.replace("_", "-").lower())
    status = "alias_mapping" if root in PY_ALIAS else "direct_or_normalized_mapping"
    return root, package, status


def normalize_rust_import(raw_import: str) -> tuple[str, str, str]:
    root = raw_import.split("::")[0]
    if root in {"crate", "self", "super", "std", "core", "alloc"}:
        return root, "", "stdlib_or_builtin"
    return root, root.replace("_", "-"), "hyphen_underscore_mapping"


def normalize_jvm_import(raw_import: str) -> tuple[str, str, str]:
    if raw_import.startswith(JAVA_STDLIB_PREFIXES) or raw_import.startswith(KOTLIN_STDLIB_PREFIXES):
        return raw_import.split(".")[0], "", "stdlib_or_builtin"
    parts = raw_import.split(".")
    root = ".".join(parts[:3]) if len(parts) >= 3 else raw_import
    return root, "", "import_to_package_mapping_unknown"


def normalize_go_import(raw_import: str) -> tuple[str, str, str]:
    if raw_import.startswith((".", "/")):
        return raw_import, "", "relative_or_local"
    first = raw_import.split("/")[0]
    if first in GO_STDLIB_ROOTS or "." not in first:
        return first, "", "stdlib_or_builtin"
    return raw_import, raw_import, "go_module_path_mapping"


def normalize_path_like(raw_import: str, status: str) -> tuple[str, str, str]:
    if raw_import.startswith((".", "/")):
        return raw_import, "", "relative_or_local"
    root = raw_import.split("/")[0]
    return root, root, status


def normalize_namespace_unknown(raw_import: str, status: str) -> tuple[str, str, str]:
    normalized = raw_import.replace("\\", ".")
    root = ".".join(normalized.split(".")[:2])
    return root, "", status


def normalize_dart_import(raw_import: str) -> tuple[str, str, str]:
    if raw_import.startswith("dart:"):
        return raw_import, "", "stdlib_or_builtin"
    if raw_import.startswith((".", "/")):
        return raw_import, "", "relative_or_local"
    if raw_import.startswith("package:"):
        package = raw_import.removeprefix("package:").split("/")[0]
        return package, package, "direct_mapping"
    return raw_import, "", "import_to_package_mapping_unknown"


def normalize_module_unknown(raw_import: str, status: str) -> tuple[str, str, str]:
    return raw_import.split(".")[0], "", status


def normalize_module_direct(raw_import: str) -> tuple[str, str, str]:
    root = raw_import.split(".")[0]
    return root, root, "direct_or_normalized_mapping"


def normalize_dotnet_import(raw_import: str) -> tuple[str, str, str]:
    if raw_import.startswith(CSHARP_STDLIB_PREFIXES):
        return raw_import.split(".")[0], "", "stdlib_or_builtin"
    return raw_import, "", "dotnet_namespace_mapping_unknown"


def normalize_solidity_import(raw_import: str) -> tuple[str, str, str]:
    if raw_import.startswith((".", "/")):
        return raw_import, "", "relative_or_local"
    parts = raw_import.split("/")
    package = "/".join(parts[:2]) if raw_import.startswith("@") and len(parts) > 1 else parts[0]
    return package, package, "npm_style_mapping"


def normalize_include_import(raw_import: str) -> tuple[str, str, str]:
    value = raw_import.strip()
    if value in CPP_STDLIB_HEADERS or value.startswith(("sys/", "linux/", "windows.h", "winsock")):
        return value.split("/")[0], "", "stdlib_or_builtin"
    if value.startswith((".", "/")):
        return value, "", "relative_or_local"
    root = value.split("/")[0]
    return root, "", "native_include_mapping_unknown"


def normalize_zig_import(raw_import: str) -> tuple[str, str, str]:
    if raw_import == "std":
        return raw_import, "", "stdlib_or_builtin"
    return raw_import, "", "zig_import_mapping_unknown"


def infer_manifest_paths(language: str, files: list[str]) -> tuple[str, str]:
    manifests, locks = manifest_candidates(language)
    touched_manifest = any(Path(f).name.lower() in manifests for f in files)
    touched_lock = any(Path(f).name.lower() in locks for f in files)
    return str(touched_manifest).lower(), str(touched_lock).lower()


def manifest_candidates(language: str) -> tuple[set[str], set[str]]:
    if language in {"JavaScript", "TypeScript", "TSX", "Vue", "Svelte", "Astro", "Solidity"}:
        return {"package.json"}, {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
    if language == "Python":
        return {"requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "pipfile"}, {"poetry.lock",
                                                                                            "pipfile.lock", "uv.lock"}
    if language == "Rust":
        return {"cargo.toml"}, {"cargo.lock"}
    if language in {"Java", "Kotlin", "Scala", "Groovy", "Clojure"}:
        return {"pom.xml", "build.gradle", "build.gradle.kts"}, {"gradle.lockfile"}
    if language == "Go":
        return {"go.mod"}, {"go.sum"}
    if language == "Ruby":
        return {"gemfile"}, {"gemfile.lock"}
    if language == "PHP":
        return {"composer.json"}, {"composer.lock"}
    if language == "Dart":
        return {"pubspec.yaml"}, {"pubspec.lock"}
    if language in {"C#", "F#"}:
        return {"packages.config", "paket.dependencies", "directory.packages.props"}, {"packages.lock.json",
                                                                                       "paket.lock"}
    if language in {"C", "C++", "C_C++_Header", "Objective-C"}:
        return {"vcpkg.json", "conanfile.txt", "conanfile.py"}, {"vcpkg-lock.json", "conan.lock"}
    if language == "Swift":
        return {"package.swift", "podfile", "cartfile"}, {"package.resolved", "podfile.lock", "cartfile.resolved"}
    if language == "R":
        return {"description"}, {"renv.lock"}
    if language in {"Elixir", "Erlang"}:
        return {"mix.exs", "rebar.config"}, {"mix.lock", "rebar.lock"}
    return set(), set()


def load_changed_files(json_path: str) -> list[str]:
    path = Path(json_path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    return [str(item.get("filename", "")) for item in data.get("files", []) if item.get("filename")]


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def event_to_row(event: ImportEvent) -> dict[str, str]:
    return {
        "repo": event.repo,
        "sha": event.sha,
        "author_date": event.author_date,
        "agent": event.agent,
        "repo_language": event.repo_language,
        "ecosystem": event.ecosystem,
        "source_file": event.source_file,
        "import_raw": event.import_raw,
        "import_root": event.import_root,
        "package_candidate": event.package_candidate,
        "import_kind": event.import_kind,
        "mapping_status": event.mapping_status,
    }


def build_candidate_row(row: dict[str, str], meta: dict[str, str], events: list[ImportEvent], files: list[str]) -> dict[
    str, str]:
    manifest_touched, lock_touched = infer_manifest_paths(row.get("repo_language", ""), files)
    external_count = sum(1 for event in events if event.import_kind == "external_candidate")
    return {
        "repo": row.get("repo", ""),
        "sha": row.get("sha", ""),
        "author_date": meta.get("author_date", ""),
        "agent": meta.get("agent", ""),
        "repo_language": row.get("repo_language", ""),
        "commit_primary_language": row.get("commit_primary_language", ""),
        "patch_path": row.get("patch_path", ""),
        "json_path": row.get("json_path", ""),
        "import_events": str(len(events)),
        "external_import_candidates": str(external_count),
        "manifest_touched_in_diff": manifest_touched,
        "lockfile_touched_in_diff": lock_touched,
        "touches_supply_chain": row.get("touches_supply_chain", ""),
    }


def row_language_allowed(row: dict[str, str], args: argparse.Namespace, exclude: set[str]) -> bool:
    language = row.get("repo_language", "")
    if language in exclude:
        return False
    if args.all_languages:
        return bool(patterns_for_language(language))
    return language in set(args.languages)


def select_candidates(args: argparse.Namespace) -> tuple[
    list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    if args.sampling_mode == "first":
        return select_candidates_first(args)
    return select_candidates_random(args)


def select_candidates_first(args: argparse.Namespace) -> tuple[
    list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    population = load_population(args.population_csv)
    checkpoint_writer = stage1_checkpoint_writer(args)
    selected, imports, audit, completed_keys, resume_after_index = load_stage1_resume(args)
    per_language = Counter(row.get("repo_language", "") for row in selected)
    exclude = set(args.exclude_languages)
    fixed_languages = set(args.languages)
    last_processed_index = resume_after_index
    for row in iter_index_rows(args.commit_index, args.max_index_rows):
        position = int(row.get("_stage1_index_position") or 0)
        if resume_after_index and position <= resume_after_index:
            continue
        language = row.get("repo_language", "")
        if not row_language_allowed(row, args, exclude):
            continue
        key = f"{row.get('repo', '')}|{row.get('sha', '')}"
        if key in completed_keys:
            continue
        last_processed_index = position
        audit_counter(audit, language)["ok_commits_seen"] += 1
        if args.sample_per_language and per_language[language] >= args.sample_per_language:
            continue
        meta = population.get(key, {})
        events, files = extract_patch_evidence_from_path(row, meta)
        if events:
            audit_counter(audit, language)["import_like_commits"] += 1
        external = [event for event in events if event.import_kind == "external_candidate"]
        if not external:
            continue
        audit_counter(audit, language)["external_candidate_commits"] += 1
        if str(row.get("touches_supply_chain", "")).lower() == "true":
            audit_counter(audit, language)["external_candidate_touch_supply_true"] += 1
        selected.append(build_candidate_row(row, meta, events, files))
        imports.extend(event_to_row(event) for event in events)
        completed_keys.add(key)
        per_language[language] += 1
        checkpoint_writer(selected, imports, audit, processed_index_rows=last_processed_index)
        if args.max_total_candidates and len(selected) >= args.max_total_candidates:
            break
        if args.sample_per_language and not args.all_languages and all(per_language[lang] >= args.sample_per_language for lang in fixed_languages):
            break
    checkpoint_writer(selected, imports, audit, force=True, processed_index_rows=last_processed_index)
    setattr(args, "_stage1_processed_index_rows", last_processed_index)
    return selected, imports, audit_rows(audit, selected)


def select_candidates_random(args: argparse.Namespace) -> tuple[
    list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    population = load_population(args.population_csv)
    rng = random.Random(args.random_seed)
    reservoirs: dict[str, list[tuple[dict[str, str], dict[str, str], list[ImportEvent], list[str]]]] = {}
    eligible_counts = Counter()
    audit = defaultdict_counter()
    exclude = set(args.exclude_languages)
    for row in iter_index_rows(args.commit_index, args.max_index_rows):
        language = row.get("repo_language", "")
        if not row_language_allowed(row, args, exclude):
            continue
        audit_counter(audit, language)["ok_commits_seen"] += 1
        key = f"{row.get('repo', '')}|{row.get('sha', '')}"
        meta = population.get(key, {})
        events, files = extract_patch_evidence_from_path(row, meta)
        if events:
            audit_counter(audit, language)["import_like_commits"] += 1
        external = [event for event in events if event.import_kind == "external_candidate"]
        if not external:
            continue
        audit_counter(audit, language)["external_candidate_commits"] += 1
        if str(row.get("touches_supply_chain", "")).lower() == "true":
            audit_counter(audit, language)["external_candidate_touch_supply_true"] += 1
        eligible_counts[language] += 1
        bucket = reservoirs.setdefault(language, [])
        item = (dict(row), dict(meta), events, files)
        if not args.sample_per_language:
            bucket.append(item)
        elif len(bucket) < args.sample_per_language:
            bucket.append(item)
        else:
            pos = rng.randrange(eligible_counts[language])
            if pos < args.sample_per_language:
                bucket[pos] = item
    selected: list[dict[str, str]] = []
    imports: list[dict[str, str]] = []
    for language in sorted(reservoirs):
        for row, meta, events, files in reservoirs[language]:
            selected.append(build_candidate_row(row, meta, events, files))
            imports.extend(event_to_row(event) for event in events)
            if args.max_total_candidates and len(selected) >= args.max_total_candidates:
                return selected, imports, audit_rows(audit, selected)
    return selected, imports, audit_rows(audit, selected)


def defaultdict_counter() -> dict[str, Counter]:
    return {}


def audit_counter(audit: dict[str, Counter], language: str) -> Counter:
    if language not in audit:
        audit[language] = Counter()
    return audit[language]


def load_stage1_resume(args: argparse.Namespace) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, Counter], set[str], int]:
    if not getattr(args, "resume_stage1", True) or args.sampling_mode != "first":
        return [], [], defaultdict_counter(), set(), 0
    checkpoint_path = args.output_dir / "stage1_checkpoint.json"
    if not checkpoint_path.exists():
        return [], [], defaultdict_counter(), set(), 0
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return [], [], defaultdict_counter(), set(), 0
    if checkpoint.get("complete"):
        return [], [], defaultdict_counter(), set(), 0
    selected = read_csv(args.output_dir / "candidate_commits.csv")
    imports = read_csv(args.output_dir / "import_events.csv")
    if not selected:
        return [], [], defaultdict_counter(), set(), 0
    audit = audit_from_rows(read_csv(args.output_dir / "sampling_audit.csv"))
    completed = {f"{row.get('repo', '')}|{row.get('sha', '')}" for row in selected}
    resume_after = int(checkpoint.get("processed_index_rows") or 0)
    mode = "index_offset" if resume_after else "dedupe_existing_candidates"
    print(
        f"stage1_resume mode={mode} candidates={len(selected)} import_events={len(imports)} "
        f"processed_index_rows={resume_after} output_dir={args.output_dir}",
        flush=True,
    )
    return selected, imports, audit, completed, resume_after


def audit_from_rows(rows: list[dict[str, str]]) -> dict[str, Counter]:
    audit = defaultdict_counter()
    fields = ["ok_commits_seen", "import_like_commits", "external_candidate_commits", "external_candidate_touch_supply_true"]
    for row in rows:
        language = row.get("repo_language", "")
        if not language:
            continue
        counter = audit_counter(audit, language)
        for field in fields:
            try:
                counter[field] = int(row.get(field, "") or 0)
            except ValueError:
                counter[field] = 0
    return audit


def audit_rows(audit: dict[str, Counter], selected: list[dict[str, str]]) -> list[dict[str, str]]:
    sampled = Counter(row.get("repo_language", "") for row in selected)
    sampled_touch = Counter(
        row.get("repo_language", "") for row in selected if str(row.get("touches_supply_chain", "")).lower() == "true"
    )
    rows = []
    for language in sorted(audit):
        counts = audit[language]
        rows.append(
            {
                "repo_language": language,
                "ok_commits_seen": str(counts.get("ok_commits_seen", 0)),
                "import_like_commits": str(counts.get("import_like_commits", 0)),
                "external_candidate_commits": str(counts.get("external_candidate_commits", 0)),
                "external_candidate_touch_supply_true": str(counts.get("external_candidate_touch_supply_true", 0)),
                "sampled_commits": str(sampled[language]),
                "sampled_touch_supply_true": str(sampled_touch[language]),
                "sampling_mode": "",
                "sample_per_language": "",
                "random_seed": "",
                "max_index_rows": "",
                "max_total_candidates": "",
            }
        )
    return rows


def add_audit_metadata(rows: list[dict[str, str]], args: argparse.Namespace) -> list[dict[str, str]]:
    for row in rows:
        row["sampling_mode"] = args.sampling_mode
        row["sample_per_language"] = str(args.sample_per_language)
        row["random_seed"] = str(args.random_seed)
        row["max_index_rows"] = str(args.max_index_rows)
        row["max_total_candidates"] = str(args.max_total_candidates)
    return rows


def build_resolution_rows(import_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = []
    for row in import_rows:
        rows.append(
            {
                **row,
                "manifest_status": "not_fetched_no_network",
                "declared_dependency_match": "not_checked",
                "version_spec": "",
                "resolved_version": "",
                "resolution_source": "",
                "registry_status": "not_queried",
                "publish_time": "",
                "publish_before_author_date": "",
                "advisory_ids": "",
                "final_label": "pending_manifest_snapshot",
            }
        )
    return rows



def stage1_field_lists() -> tuple[list[str], list[str], list[str], list[str]]:
    candidate_fields = [
        "repo", "sha", "author_date", "agent", "repo_language", "commit_primary_language", "patch_path", "json_path",
        "import_events", "external_import_candidates", "manifest_touched_in_diff", "lockfile_touched_in_diff",
        "touches_supply_chain",
    ]
    import_fields = list(event_to_row(ImportEvent("", "", "", "", "", "", "", "", "", "", "", "")).keys())
    resolution_fields = import_fields + [
        "manifest_status", "declared_dependency_match", "version_spec", "resolved_version", "resolution_source",
        "registry_status", "publish_time", "publish_before_author_date", "advisory_ids", "final_label",
    ]
    audit_fields = ["repo_language", "ok_commits_seen", "import_like_commits", "external_candidate_commits",
                    "external_candidate_touch_supply_true", "sampled_commits", "sampled_touch_supply_true",
                    "sampling_mode", "sample_per_language", "random_seed", "max_index_rows", "max_total_candidates"]
    return candidate_fields, import_fields, resolution_fields, audit_fields


def write_stage1_outputs(output_dir: Path, candidates: list[dict[str, str]], imports: list[dict[str, str]], audit: list[dict[str, str]]) -> None:
    candidate_fields, import_fields, resolution_fields, audit_fields = stage1_field_lists()
    resolution_rows = build_resolution_rows(imports)
    write_csv(output_dir / "sampling_audit.csv", audit, audit_fields)
    write_csv(output_dir / "candidate_commits.csv", candidates, candidate_fields)
    write_csv(output_dir / "import_events.csv", imports, import_fields)
    write_csv(output_dir / "dependency_resolution.csv", resolution_rows, resolution_fields)
    write_report(output_dir, candidates, imports)


def existing_stage1_candidate_count(args: argparse.Namespace) -> int:
    if not getattr(args, "resume_stage1", True) or args.sampling_mode != "first":
        return 0
    checkpoint_path = args.output_dir / "stage1_checkpoint.json"
    if not checkpoint_path.exists():
        return 0
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return 0
    if checkpoint.get("complete"):
        return 0
    try:
        return int(checkpoint.get("selected_candidates") or 0)
    except ValueError:
        return 0


def stage1_checkpoint_writer(args: argparse.Namespace):
    every = max(0, int(getattr(args, "checkpoint_every_candidates", 0) or 0))
    last_count = existing_stage1_candidate_count(args)

    def write_checkpoint(
        selected: list[dict[str, str]],
        imports: list[dict[str, str]],
        audit: dict[str, Counter],
        force: bool = False,
        processed_index_rows: int = 0,
    ) -> None:
        nonlocal last_count
        if not every:
            return
        count = len(selected)
        if not force and count - last_count < every:
            return
        output_dir = args.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        audit_with_meta = add_audit_metadata(audit_rows(audit, selected), args)
        write_stage1_outputs(output_dir, selected, imports, audit_with_meta)
        checkpoint = {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "selected_candidates": count,
            "import_events": len(imports),
            "checkpoint_every_candidates": every,
            "processed_index_rows": processed_index_rows,
            "resume_supported": True,
            "complete": False,
        }
        (output_dir / "stage1_checkpoint.json").write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
        last_count = count
        print(f"stage1_checkpoint candidates={count} import_events={len(imports)} output_dir={output_dir}", flush=True)

    return write_checkpoint


def write_report(output_dir: Path, candidates: list[dict[str, str]], imports: list[dict[str, str]]) -> None:
    by_lang = Counter(row["repo_language"] for row in candidates)
    imports_by_lang = Counter(row["repo_language"] for row in imports)
    external = sum(1 for row in imports if row["import_kind"] == "external_candidate")
    lines = [
        "# RQ3 Hallucinated Dependency Pilot Report",
        "",
        "Mode: no-network static import extraction.",
        "",
        f"- Candidate commits: {len(candidates)}",
        f"- Import events: {len(imports)}",
        f"- External import candidates: {external}",
        "",
        "## Candidate Commits by Language",
        "",
        "| Language | Commits | Import events |",
        "|---|---:|---:|",
    ]
    for lang, count in by_lang.most_common():
        lines.append(f"| {lang} | {count} | {imports_by_lang[lang]} |")
    lines.extend(
        [
            "",
            "## Next Step",
            "",
            "Fetch child manifest/lockfile snapshots for these commits, parse declared dependencies, "
            "then resolve exact versions before querying registries or advisories.",
            "",
        ]
    )
    (output_dir / "rq2_hallucinated_pilot_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    candidates, import_rows, audit = select_candidates(args)
    audit = add_audit_metadata(audit, args)
    write_stage1_outputs(args.output_dir, candidates, import_rows, audit)
    checkpoint = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "selected_candidates": len(candidates),
        "import_events": len(import_rows),
        "checkpoint_every_candidates": int(getattr(args, "checkpoint_every_candidates", 0) or 0),
        "processed_index_rows": int(getattr(args, "_stage1_processed_index_rows", 0) or 0),
        "resume_supported": True,
        "complete": True,
    }
    (args.output_dir / "stage1_checkpoint.json").write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"candidate_commits={len(candidates)}")
    print(f"import_events={len(import_rows)}")
    print(f"output_dir={args.output_dir}")


if __name__ == "__main__":
    main()



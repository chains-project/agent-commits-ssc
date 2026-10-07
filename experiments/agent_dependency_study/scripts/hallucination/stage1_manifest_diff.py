"""Parse RQ2 Stage 1 source imports and direct manifest additions from diffs.

[IN]: Unified Git diff lines for commits in the six-language RQ2 scope.
[OUT]: File/hunk lines with new-file positions and compact external import or
direct manifest-addition observations suitable for events.csv.
[POS]: Pure, offline Stage 1 parser; it does not sample commits, write files,
read registries, parse lockfiles, or perform Stage 2 dependency alignment.
[SYNC]: Keep stage1_extract.py, tests/test_rq2_stage1_manifest_diff.py,
tests/fixtures/rq2_synthetic_pipeline_v2/scenarios.json, scripts/OUTPUTS.md,
and dependency-experiment-design.md synchronized when supported files or
event semantics change.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from pypi_requirement import without_environment_marker
from stage_schema import SCHEMA_VERSION, make_event_id


TARGET_SOURCE_LANGUAGES = (
    "TypeScript",
    "Python",
    "JavaScript",
    "Rust",
    "Go",
    "Java",
)

DIFF_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$")
NEW_FILE_HEADER = re.compile(r"^\+\+\+ b/(.*)$")
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
JSON_PAIR = re.compile(r'^\s*"([^"]+)"\s*:\s*"([^"]*)"\s*,?\s*$')
TOML_PAIR = re.compile(r"^\s*['\"]?([A-Za-z0-9_.-]+)['\"]?\s*=\s*(.+?)\s*$")
REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[^\]]+\])?)\s*(.*)$")
GO_REQUIRE = re.compile(r"^\s*(?:require\s+)?([^\s]+)\s+(v[^\s]+)(?:\s+//.*)?$")
XML_VALUE = r"<%s>\s*([^<]+?)\s*</%s>"

NPM_DEPENDENCY_SECTIONS = {
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
}
NON_RESEARCH_PATH_SEGMENTS = frozenset(
    {"node_modules", "vendor", "vendored", "generated"}
)
CARGO_DEPENDENCY_SECTION = re.compile(
    r"^\[(?:target\.[^]]+\.)?(?:dev-|build-)?dependencies\]$",
    re.IGNORECASE,
)
GRADLE_CONFIGURATION = re.compile(
    r'(?<![\w.])"?(?:api|implementation|compile|runtime|provided|thirdParty|'
    r'compileOnly|runtimeOnly|annotationProcessor|classpath|'
    r'[A-Za-z_]\w*(?:Api|Implementation|CompileOnly|RuntimeOnly|Compile|Runtime|'
    r'AnnotationProcessor))"?'
    r'(?:\s*\(|\s+(?=["\']|\bproject\s*\(|'
    r'\b(?:platform|enforcedPlatform|fg\.deobf|include)\s*\(|\blibs(?:\.|\b)|\bgroup\s*[:=]))'
)
GRADLE_LOCAL_PROJECT = re.compile(r"\bproject\s*\(")
GRADLE_COMMENT = re.compile(r"^\s*(?://|/\*|\*|\*/|#)")
GRADLE_COORDINATE = re.compile(
    r"['\"](?P<group>[A-Za-z0-9_.${}-]+):"
    r"(?P<artifact>[A-Za-z0-9_.${}-]+):(?P<version>[^'\"]+)['\"]"
)

NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "module", "net", "os",
    "path", "perf_hooks", "process", "punycode", "querystring", "readline",
    "repl", "stream", "string_decoder", "sys", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib",
})
JAVASCRIPT_PROTOCOL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
NPM_PACKAGE_NAME = re.compile(
    r"^(?:@[A-Za-z0-9][A-Za-z0-9._~-]*/)?[A-Za-z0-9][A-Za-z0-9._~-]*$"
)
WORKSPACE_IMPORT_PREFIXES = ("$", "@workspace/", "@repo/", "@packages/")
from python_stdlib import KNOWN_STDLIB_ROOTS, needs_stdlib_context

PYTHON_STDLIB_FALLBACK = KNOWN_STDLIB_ROOTS
PYTHON_STDLIB = KNOWN_STDLIB_ROOTS
PYTHON_ALIASES = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "PIL": "pillow",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
}
GO_STDLIB_ROOTS = {
    "archive", "bufio", "bytes", "compress", "context", "crypto", "database",
    "embed", "encoding", "errors", "expvar", "flag", "fmt", "hash", "html",
    "image", "index", "io", "log", "math", "mime", "net", "os", "path",
    "reflect", "regexp", "runtime", "sort", "strconv", "strings", "sync",
    "testing", "text", "time", "unicode",
}


@dataclass(frozen=True)
class PatchLine:
    new_line_number: int
    hunk_index: int
    text: str
    is_added: bool


@dataclass(frozen=True)
class FilePatch:
    path: str
    lines: tuple[PatchLine, ...]

    @property
    def added_lines(self) -> tuple[PatchLine, ...]:
        return tuple(line for line in self.lines if line.is_added)


@dataclass(frozen=True)
class ManifestDescriptor:
    ecosystem: str
    actual_language: str
    parser: str


@dataclass(frozen=True)
class Stage1Observation:
    event_type: str
    path: str
    actual_language: str
    ecosystem: str
    new_line_number: int
    event_ordinal: int
    raw_target: str
    package_candidate: str
    version_spec: str
    event_status: str
    reason_codes: tuple[str, ...] = ()

    def to_event_row(self, commit_id: str) -> dict[str, str]:
        event_id = make_event_id(
            commit_id,
            self.event_type,
            self.path,
            self.new_line_number,
            self.event_ordinal,
            self.raw_target,
        )
        return {
            "event_id": event_id,
            "commit_id": commit_id,
            "event_type": self.event_type,
            "path": self.path,
            "actual_language": self.actual_language,
            "ecosystem": self.ecosystem,
            "new_line_number": str(self.new_line_number),
            "event_ordinal": str(self.event_ordinal),
            "raw_target": self.raw_target,
            "package_candidate": self.package_candidate,
            "version_spec": self.version_spec,
            "event_status": self.event_status,
            "reason_codes": "|".join(self.reason_codes),
            "schema_version": str(SCHEMA_VERSION),
        }


def parse_unified_diff(lines: Iterable[str]) -> list[FilePatch]:
    files: list[FilePatch] = []
    path = ""
    patch_lines: list[PatchLine] = []
    new_line = 0
    hunk_index = 0
    for raw_line in lines:
        raw = raw_line.rstrip("\r\n")
        match = DIFF_HEADER.match(raw)
        if match:
            _append_file(files, path, patch_lines)
            path, patch_lines = match.group(2), []
            new_line, hunk_index = 0, 0
            continue
        path = _updated_path(raw, path)
        hunk = HUNK_HEADER.match(raw)
        if hunk:
            new_line, hunk_index = int(hunk.group(1)), hunk_index + 1
            continue
        new_line = _append_patch_line(patch_lines, raw, new_line, hunk_index)
    _append_file(files, path, patch_lines)
    return files


def _append_file(files: list[FilePatch], path: str, lines: list[PatchLine]) -> None:
    if path and path != "/dev/null":
        files.append(FilePatch(path, tuple(lines)))


def _updated_path(raw: str, current: str) -> str:
    match = NEW_FILE_HEADER.match(raw)
    if match and match.group(1) != "/dev/null":
        return match.group(1)
    return current


def _append_patch_line(
    lines: list[PatchLine], raw: str, new_line: int, hunk_index: int
) -> int:
    if not hunk_index or raw.startswith("\\ No newline"):
        return new_line
    if raw.startswith("+") and not raw.startswith("+++"):
        lines.append(PatchLine(new_line, hunk_index, raw[1:], True))
        return new_line + 1
    if raw.startswith("-") and not raw.startswith("---"):
        return new_line
    if raw.startswith(" "):
        lines.append(PatchLine(new_line, hunk_index, raw[1:], False))
        return new_line + 1
    return new_line


def detect_source_language(path: str) -> str:
    lower = path.lower().split("?", 1)[0]
    suffix = Path(lower).suffix
    if suffix in {".ts", ".tsx", ".mts", ".cts"}:
        return "TypeScript"
    if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        return "JavaScript"
    if suffix in {".py", ".pyw"}:
        return "Python"
    if suffix == ".rs":
        return "Rust"
    if suffix == ".go":
        return "Go"
    if suffix == ".java":
        return "Java"
    return ""


def is_research_file_path(path: str) -> bool:
    normalized = path.replace("\\", "/").split("?", 1)[0]
    segments = {part.lower() for part in normalized.split("/") if part}
    return not bool(segments & NON_RESEARCH_PATH_SEGMENTS)


def source_ecosystem(language: str) -> str:
    return {
        "TypeScript": "npm",
        "JavaScript": "npm",
        "Python": "PyPI",
        "Rust": "Cargo",
        "Go": "Go",
        "Java": "Maven",
    }.get(language, "")


def manifest_descriptor(path: str) -> ManifestDescriptor | None:
    name = Path(path).name.lower()
    if name == "package.json":
        return ManifestDescriptor("npm", "JSON", "npm_json")
    if name.startswith("requirements") and name.endswith(".txt"):
        return ManifestDescriptor("PyPI", "Requirements", "requirements")
    if name == "pyproject.toml":
        return ManifestDescriptor("PyPI", "TOML", "pyproject")
    if name == "setup.cfg":
        return ManifestDescriptor("PyPI", "INI", "setup_cfg")
    if name == "cargo.toml":
        return ManifestDescriptor("Cargo", "TOML", "cargo")
    if name == "go.mod":
        return ManifestDescriptor("Go", "Go Module", "go_mod")
    if name == "pom.xml":
        return ManifestDescriptor("Maven", "XML", "maven")
    if name in {"build.gradle", "build.gradle.kts"}:
        return ManifestDescriptor("Maven", "Gradle", "gradle")
    if name == "libs.versions.toml":
        return ManifestDescriptor("Maven", "TOML", "gradle_catalog")
    return None


def extract_stage1_observations(lines: Iterable[str]) -> list[Stage1Observation]:
    observations: list[Stage1Observation] = []
    for file_patch in parse_unified_diff(lines):
        if not is_research_file_path(file_patch.path):
            continue
        observations.extend(extract_source_imports(file_patch))
        observations.extend(extract_manifest_additions(file_patch))
    return observations


def extract_source_imports(file_patch: FilePatch) -> list[Stage1Observation]:
    if not is_research_file_path(file_patch.path):
        return []
    language = detect_source_language(file_patch.path)
    if not language:
        return []
    observations: list[Stage1Observation] = []
    go_block = False
    current_hunk = 0
    for line in file_patch.lines:
        if line.hunk_index != current_hunk:
            current_hunk, go_block = line.hunk_index, False
        targets, go_block = _import_targets(line.text, language, go_block)
        if not line.is_added:
            continue
        for ordinal, target in enumerate(targets):
            event = _source_observation(file_patch.path, line, ordinal, language, target)
            if event is not None:
                observations.append(event)
    return observations


def _import_targets(text: str, language: str, go_block: bool) -> tuple[list[str], bool]:
    if language in {"TypeScript", "JavaScript"}:
        return _javascript_imports(text), go_block
    if language == "Python":
        return _python_imports(text), go_block
    if language == "Rust":
        return _regex_targets(text, (r"^\s*use\s+([A-Za-z_][\w:]*)", r"^\s*extern\s+crate\s+([A-Za-z_]\w*)")), go_block
    if language == "Java":
        return _regex_targets(text, (r"^\s*import\s+(?:static\s+)?([A-Za-z_]\w*(?:\.[A-Za-z_*]\w*)+)\s*;?",)), go_block
    if language == "Go":
        return _go_imports(text, go_block)
    return [], go_block


def _javascript_imports(text: str) -> list[str]:
    patterns = (
        r"\bfrom\s*['\"]([^'\"]+)['\"]",
        r"^\s*import\s*['\"]([^'\"]+)['\"]",
        r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)",
        r"\bimport\(\s*['\"]([^'\"]+)['\"]\s*\)",
    )
    matches = []
    for pattern in patterns:
        matches.extend(
            (match.start(), match.group(1))
            for match in re.finditer(pattern, text)
            if match.start() == 0 or text[match.start() - 1] != '-'
        )
    return [target for _position, target in sorted(set(matches))]


def _python_imports(text: str) -> list[str]:
    try:
        tree = ast.parse(text.strip())
    except SyntaxError:
        return []
    targets: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            targets.append("." * node.level + (node.module or ""))
    return targets


def _regex_targets(text: str, patterns: Sequence[str]) -> list[str]:
    targets = []
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            targets.append(match.group(1))
    return targets


def _go_imports(text: str, in_block: bool) -> tuple[list[str], bool]:
    stripped = text.strip()
    if stripped.startswith("import ("):
        return [], True
    if in_block and stripped == ")":
        return [], False
    pattern = r'^\s*import\s+(?:[._A-Za-z]\w*\s+)?["`]([^"`]+)["`]'
    if in_block:
        pattern = r'^\s*(?:[._A-Za-z]\w*\s+)?["`]([^"`]+)["`]'
    match = re.search(pattern, text)
    return ([match.group(1)] if match else []), in_block


def _source_observation(
    path: str, line: PatchLine, ordinal: int, language: str, target: str
) -> Stage1Observation | None:
    if not target.strip():
        return None
    package, status, reasons = _normalize_source_target(language, target)
    if status in {"stdlib_or_builtin", "relative_or_local", "protocol_or_virtual"}:
        return None
    return Stage1Observation(
        "import", path, language, source_ecosystem(language),
        line.new_line_number, ordinal, target, package, "", status, reasons,
    )


def _normalize_source_target(
    language: str, target: str
) -> tuple[str, str, tuple[str, ...]]:
    if language in {"TypeScript", "JavaScript"}:
        return _normalize_javascript(target)
    if language == "Python":
        return _normalize_python(target)
    if language == "Rust":
        return _normalize_rust(target)
    if language == "Go":
        return _normalize_go(target)
    if language == "Java":
        if target.startswith(("java.", "javax.")):
            return "", "stdlib_or_builtin", ()
        return "", "mapping_uncertain", ("mapping_uncertain",)
    return "", "mapping_uncertain", ("mapping_uncertain",)


def _normalize_javascript(target: str) -> tuple[str, str, tuple[str, ...]]:
    if target.startswith((".", "/", "#", "@/", "~/")):
        return "", "relative_or_local", ()
    if target.startswith("node:"):
        return "", "stdlib_or_builtin", ()
    if JAVASCRIPT_PROTOCOL.match(target):
        return "", "protocol_or_virtual", ()
    if target.startswith(WORKSPACE_IMPORT_PREFIXES):
        return "", "mapping_uncertain", ("possible_workspace_alias",)
    parts = target.split("/")
    if parts[0] in NODE_BUILTINS:
        return "", "stdlib_or_builtin", ()
    package = "/".join(parts[:2]) if target.startswith("@") else parts[0]
    if not NPM_PACKAGE_NAME.fullmatch(package):
        return "", "mapping_uncertain", ("invalid_npm_package_candidate",)
    return package, "direct_mapping", ()


def _normalize_python(target: str) -> tuple[str, str, tuple[str, ...]]:
    if target.startswith("."):
        return "", "relative_or_local", ()
    root = target.split(".")[0]
    if root in PYTHON_STDLIB or root.startswith("_"):
        return "", "stdlib_or_builtin", ()
    if needs_stdlib_context(root):
        return "", "mapping_uncertain", ("stdlib_reference_requires_runtime_context",)
    package = PYTHON_ALIASES.get(root, root.replace("_", "-").lower())
    status = "alias_mapping" if root in PYTHON_ALIASES else "direct_or_normalized_mapping"
    return package, status, ()


def _normalize_rust(target: str) -> tuple[str, str, tuple[str, ...]]:
    root = target.split("::")[0]
    if root in {"crate", "self", "super", "std", "core", "alloc"}:
        return "", "stdlib_or_builtin", ()
    return root.replace("_", "-"), "hyphen_underscore_mapping", ()


def _normalize_go(target: str) -> tuple[str, str, tuple[str, ...]]:
    if target.startswith((".", "/")):
        return "", "relative_or_local", ()
    root = target.split("/")[0]
    if root in GO_STDLIB_ROOTS or "." not in root:
        return "", "stdlib_or_builtin", ()
    return target, "go_module_path_mapping", ()


def extract_manifest_additions(file_patch: FilePatch) -> list[Stage1Observation]:
    if not is_research_file_path(file_patch.path):
        return []
    descriptor = manifest_descriptor(file_patch.path)
    if descriptor is None:
        return []
    parser = {
        "npm_json": _parse_npm_json,
        "requirements": _parse_requirements,
        "pyproject": _parse_pyproject,
        "setup_cfg": _parse_setup_cfg,
        "cargo": _parse_cargo,
        "go_mod": _parse_go_mod,
        "maven": _parse_maven,
        "gradle": _parse_gradle,
        "gradle_catalog": _parse_gradle_catalog,
    }[descriptor.parser]
    return parser(file_patch, descriptor)


def _manifest_observation(
    patch: FilePatch,
    descriptor: ManifestDescriptor,
    line: PatchLine,
    ordinal: int,
    package: str,
    version: str,
    status: str = "direct_manifest",
    reasons: tuple[str, ...] = (),
) -> Stage1Observation:
    return Stage1Observation(
        "manifest_addition", patch.path, descriptor.actual_language,
        descriptor.ecosystem, line.new_line_number, ordinal, package, package,
        version.strip().strip('"').strip("'"), status, reasons,
    )


def _parse_npm_json(
    patch: FilePatch, descriptor: ManifestDescriptor
) -> list[Stage1Observation]:
    events = []
    section = ""
    hunk = 0
    for line in patch.lines:
        if line.hunk_index != hunk:
            hunk, section = line.hunk_index, ""
        header = re.search(r'^\s*"([^"]+)"\s*:\s*\{', line.text)
        if header:
            section = header.group(1) if header.group(1) in NPM_DEPENDENCY_SECTIONS else ""
            continue
        if section and line.text.lstrip().startswith("}"):
            section = ""
        match = JSON_PAIR.match(line.text)
        if line.is_added and section and match:
            events.append(_manifest_observation(patch, descriptor, line, 0, match.group(1), match.group(2)))
    return events


def _parse_requirements(
    patch: FilePatch, descriptor: ManifestDescriptor
) -> list[Stage1Observation]:
    events = []
    for line in patch.added_lines:
        parsed = _requirement_parts(line.text)
        if parsed:
            package, spec = parsed
            events.append(_manifest_observation(patch, descriptor, line, 0, package, spec))
    return events


def _requirement_parts(text: str) -> tuple[str, str] | None:
    stripped = text.strip()
    if not stripped or stripped.startswith(("#", "-")):
        return None
    stripped = re.split(r"\s+#", stripped, maxsplit=1)[0].rstrip()
    match = REQUIREMENT.match(stripped)
    if not match:
        return None
    package = match.group(1).split("[", 1)[0]
    spec = without_environment_marker(match.group(2))
    if spec and not re.match(r"^(?:===|~=|==|!=|<=|>=|<|>|@|;)", spec):
        return None
    return package, spec


def _parse_pyproject(
    patch: FilePatch, descriptor: ManifestDescriptor
) -> list[Stage1Observation]:
    events = []
    section, in_array, hunk = "", False, 0
    for line in patch.lines:
        if line.hunk_index != hunk:
            hunk, section, in_array = line.hunk_index, "", False
        stripped = line.text.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section, in_array = stripped.lower(), False
            continue
        if section == "[project]" and stripped.startswith("dependencies"):
            in_array = "[" in stripped and "]" not in stripped
            events.extend(_inline_requirement_events(patch, descriptor, line))
            continue
        if in_array and stripped.startswith("]"):
            in_array = False
        event = _pyproject_line_event(patch, descriptor, line, section, in_array)
        if event:
            events.append(event)
    return events


def _inline_requirement_events(
    patch: FilePatch, descriptor: ManifestDescriptor, line: PatchLine
) -> list[Stage1Observation]:
    if not line.is_added:
        return []
    events = []
    for ordinal, value in enumerate(re.findall(r"['\"]([^'\"]+)['\"]", line.text)):
        parsed = _requirement_parts(value)
        if parsed:
            events.append(_manifest_observation(patch, descriptor, line, ordinal, *parsed))
    return events


def _pyproject_line_event(
    patch: FilePatch,
    descriptor: ManifestDescriptor,
    line: PatchLine,
    section: str,
    in_array: bool,
) -> Stage1Observation | None:
    if not line.is_added:
        return None
    if line.text.lstrip().startswith("#"):
        return None
    if section == "[tool.poetry.dependencies]":
        match = TOML_PAIR.match(line.text)
        if match and match.group(1).lower() != "python":
            return _manifest_observation(patch, descriptor, line, 0, match.group(1), match.group(2))
    if in_array:
        values = re.findall(r"['\"]([^'\"]+)['\"]", line.text)
        parsed = _requirement_parts(values[0]) if values else None
        if parsed:
            return _manifest_observation(patch, descriptor, line, 0, *parsed)
    return None


def _parse_setup_cfg(
    patch: FilePatch, descriptor: ManifestDescriptor
) -> list[Stage1Observation]:
    events = []
    section, in_requires, hunk = "", False, 0
    for line in patch.lines:
        if line.hunk_index != hunk:
            hunk, section, in_requires = line.hunk_index, "", False
        stripped = line.text.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section, in_requires = stripped.lower(), False
            continue
        option = re.match(r"^install_requires\s*=\s*(.*)$", stripped, re.IGNORECASE)
        if section == "[options]" and option:
            in_requires = True
            if line.is_added:
                parsed = _requirement_parts(option.group(1))
                if parsed:
                    events.append(_manifest_observation(patch, descriptor, line, 0, *parsed))
            continue
        if in_requires and stripped and not line.text[:1].isspace():
            in_requires = False
            continue
        if line.is_added and in_requires:
            parsed = _requirement_parts(stripped)
            if parsed:
                events.append(_manifest_observation(patch, descriptor, line, 0, *parsed))
    return events


def _parse_cargo(
    patch: FilePatch, descriptor: ManifestDescriptor
) -> list[Stage1Observation]:
    events = []
    dependency_section, hunk = False, 0
    for line in patch.lines:
        if line.hunk_index != hunk:
            hunk, dependency_section = line.hunk_index, False
        stripped = line.text.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            dependency_section = bool(CARGO_DEPENDENCY_SECTION.match(stripped))
            continue
        match = TOML_PAIR.match(line.text)
        if line.is_added and dependency_section and match:
            parsed = _cargo_dependency_parts(match.group(1), match.group(2))
            if parsed:
                package, spec, reasons = parsed
                events.append(_manifest_observation(
                    patch, descriptor, line, 0, package, spec, reasons=reasons,
                ))
    return events


def _cargo_dependency_parts(
    key: str, value: str,
) -> tuple[str, str, tuple[str, ...]] | None:
    if "." in key:
        package, field = key.rsplit(".", 1)
        if field.lower() == "workspace" and value.strip().lower() == "true":
            return package, "workspace:", ("workspace_inherited",)
        if field.lower() == "version":
            return package, _toml_version(value), ()
        return None
    if _cargo_workspace_inherited(value):
        return key, "workspace:", ("workspace_inherited",)
    return key, _toml_version(value), ()


def _cargo_workspace_inherited(value: str) -> bool:
    return bool(re.search(r"\bworkspace\s*=\s*true\b", value, re.IGNORECASE))


def _toml_version(value: str) -> str:
    stripped = value.strip().strip('"').strip("'")
    match = re.search(r"\bversion\s*=\s*['\"]([^'\"]+)['\"]", value)
    return match.group(1) if match else stripped


def _parse_go_mod(
    patch: FilePatch, descriptor: ManifestDescriptor
) -> list[Stage1Observation]:
    events = []
    in_block, hunk = False, 0
    for line in patch.lines:
        if line.hunk_index != hunk:
            hunk, in_block = line.hunk_index, False
        stripped = line.text.strip()
        if stripped.startswith("require ("):
            in_block = True
            continue
        if in_block and stripped == ")":
            in_block = False
            continue
        match = GO_REQUIRE.match(stripped)
        is_direct = in_block or stripped.startswith("require ")
        if line.is_added and is_direct and match:
            events.append(_manifest_observation(patch, descriptor, line, 0, match.group(1), match.group(2)))
    return events


def _parse_maven(
    patch: FilePatch, descriptor: ManifestDescriptor
) -> list[Stage1Observation]:
    events = []
    block: list[PatchLine] = []
    in_dependency, in_management = False, False
    for line in patch.lines:
        if "<dependencyManagement" in line.text:
            in_management = True
        if "</dependencyManagement" in line.text:
            in_management = False
        if "<dependency>" in line.text and not in_management:
            in_dependency, block = True, [line]
        elif in_dependency:
            block.append(line)
        if in_dependency and "</dependency>" in line.text:
            event = _maven_block_event(patch, descriptor, block)
            if event:
                events.append(event)
            in_dependency, block = False, []
    return events


def _maven_block_event(
    patch: FilePatch, descriptor: ManifestDescriptor, block: Sequence[PatchLine]
) -> Stage1Observation | None:
    text = "\n".join(line.text for line in block)
    group = _xml_value(text, "groupId")
    artifact = _xml_value(text, "artifactId")
    version = _xml_value(text, "version")
    identity_added = any(
        line.is_added and any(tag in line.text for tag in ("<dependency>", "<groupId>", "<artifactId>"))
        for line in block
    )
    if not group or not artifact or not identity_added:
        return None
    evidence_line = next(line for line in block if line.is_added)
    return _manifest_observation(patch, descriptor, evidence_line, 0, f"{group}:{artifact}", version)


def _xml_value(text: str, tag: str) -> str:
    match = re.search(XML_VALUE % (tag, tag), text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _parse_gradle(
    patch: FilePatch, descriptor: ManifestDescriptor
) -> list[Stage1Observation]:
    events = []
    for line in patch.added_lines:
        event = _gradle_line_event(patch, descriptor, line)
        if event is not None:
            events.append(event)
    return events


def _gradle_line_event(
    patch: FilePatch, descriptor: ManifestDescriptor, line: PatchLine,
) -> Stage1Observation | None:
    text = line.text
    if GRADLE_COMMENT.search(text) or GRADLE_LOCAL_PROJECT.search(text):
        return None
    if not GRADLE_CONFIGURATION.search(text):
        return None
    coordinate = GRADLE_COORDINATE.search(text)
    if coordinate:
        package = f"{coordinate.group('group')}:{coordinate.group('artifact')}"
        return _manifest_observation(
            patch, descriptor, line, 0, package, coordinate.group("version"),
        )
    mapped = _gradle_map_parts(text)
    if mapped:
        return _manifest_observation(patch, descriptor, line, 0, *mapped)
    alias = re.search(r"\blibs(?:\.[A-Za-z_]\w*)+", text)
    if alias:
        return _manifest_observation(
            patch, descriptor, line, 0, alias.group(0), "",
            "unresolved_gradle_alias", ("version_unresolved",),
        )
    return None


def _gradle_map_parts(text: str) -> tuple[str, str] | None:
    group = re.search(r"\bgroup\s*[:=]\s*['\"]([^'\"]+)['\"]", text)
    name = re.search(r"\bname\s*[:=]\s*['\"]([^'\"]+)['\"]", text)
    version = re.search(r"\bversion\s*[:=]\s*['\"]([^'\"]+)['\"]", text)
    if group and name:
        return f"{group.group(1)}:{name.group(1)}", version.group(1) if version else ""
    return None


def _parse_gradle_catalog(
    patch: FilePatch, descriptor: ManifestDescriptor
) -> list[Stage1Observation]:
    events = []
    in_libraries, hunk = False, 0
    for line in patch.lines:
        if line.hunk_index != hunk:
            hunk, in_libraries = line.hunk_index, False
        stripped = line.text.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_libraries = stripped.lower() == "[libraries]"
            continue
        if not line.is_added or not in_libraries:
            continue
        module = re.search(r"\bmodule\s*=\s*['\"]([^'\"]+)['\"]", line.text)
        version = re.search(r"\bversion\s*=\s*['\"]([^'\"]+)['\"]", line.text)
        if module:
            events.append(_manifest_observation(
                patch, descriptor, line, 0, module.group(1),
                version.group(1) if version else "", "direct_manifest_catalog",
            ))
    return events


def actual_changed_languages(file_patches: Iterable[FilePatch]) -> tuple[str, ...]:
    languages = {
        detect_source_language(patch.path)
        for patch in file_patches
        if patch.added_lines
        and is_research_file_path(patch.path)
        and detect_source_language(patch.path)
    }
    return tuple(language for language in TARGET_SOURCE_LANGUAGES if language in languages)


def dependency_quadrant(observations: Iterable[Stage1Observation]) -> str:
    event_types = {event.event_type for event in observations}
    has_import = "import" in event_types
    has_manifest = "manifest_addition" in event_types
    if has_import and has_manifest:
        return "I+M+"
    if has_import:
        return "I+M-"
    if has_manifest:
        return "I-M+"
    return "I-M-"


def iter_added_lines(file_patch: FilePatch) -> Iterator[PatchLine]:
    yield from file_patch.added_lines

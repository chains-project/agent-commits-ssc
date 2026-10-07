"""Collect fresh official GitHub and npm evidence for blinded RQ3 review.

[IN]: hardened blinded reviewer CSV, protocol/codebook, GitHub token, GitHub
REST/raw endpoints, and registry.npmjs.org.
[OUT]: resumable gzip response cache, case-linked JSONL request ledger,
official_evidence_matrix.csv, input baseline, manifest, and invariants.
[POS]: Independent evidence collection only; this script never assigns human
or adjudicated labels and never edits reviewer_1_form.csv/reviewer_2_form.csv.
[SYNC]: If sources, schemas, or output names change, update scripts/CLAUDE.md,
scripts/OUTPUTS.md, and the active Codex independent-review plan.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRODUCT = Path("data_products/rq2_hallucinated_strong_label_hardened_20260722")
REVIEW_ROOT = PRODUCT / "human_review"
DEFAULT_INPUT = REVIEW_ROOT / "reviewer_1_form.csv"
DEFAULT_OUTPUT = REVIEW_ROOT / "codex_independent_review"
PROTECTED_NAMES = (
    "reviewer_1_form.csv",
    "reviewer_2_form.csv",
    "calibration_reviewer_1.csv",
    "calibration_reviewer_2.csv",
    "review_protocol.md",
    "validation_codebook_v1_snapshot.md",
)
MATRIX_FIELDS = (
    "review_order", "case_id", "redacted_case_id", "language", "agent",
    "repo", "sha", "package_name", "version", "author_date",
    "form_committer_date", "form_registry_publish_time", "parent_sha",
    "parent_child_state", "local_change_class", "dependency_file_changed",
    "mapping_statuses_json", "resolution_sources_json",
    "generated_or_vendored_path_hint", "non_registry_spec_hint",
    "fresh_commit_status", "fresh_author_date", "fresh_committer_date",
    "fresh_parent_sha", "fresh_parent_count", "fresh_repo_status",
    "fresh_repo_created_at", "fresh_repo_pushed_at", "fresh_npm_status",
    "fresh_version_exists", "fresh_registry_publish_time",
    "fresh_child_file_states_json", "fresh_parent_file_states_json",
    "fresh_parent_child_states_json", "fresh_evidence_complete",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--review-root", type=Path, default=REVIEW_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--github-token-env", default="GITHUB_TOKEN")
    parser.add_argument("--request-attempts", type=int, default=5)
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fingerprint(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "path": str(path.resolve()),
        "size_bytes": len(payload),
        "sha256": sha256_bytes(payload),
    }


def load_token(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    path = Path("scripts/.env")
    if not path.exists():
        return ""
    return token_from_lines(path.read_text(encoding="utf-8").splitlines(), name)


def token_from_lines(lines: list[str], name: str) -> str:
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def repo_segments(repo: str) -> str:
    return "/".join(urllib.parse.quote(part, safe="") for part in repo.split("/"))


def commit_url(repo: str, sha: str) -> str:
    return f"https://api.github.com/repos/{repo_segments(repo)}/commits/{sha}"


def repository_url(repo: str) -> str:
    return f"https://api.github.com/repos/{repo_segments(repo)}"


def raw_url(repo: str, sha: str, path: str) -> str:
    encoded = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))
    return f"https://raw.githubusercontent.com/{repo_segments(repo)}/{sha}/{encoded}"


def npm_url(package: str) -> str:
    return f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='')}"


def cache_path(cache_dir: Path, url: str) -> Path:
    digest = sha256_bytes(url.encode("utf-8"))
    return cache_dir / digest[:2] / f"{digest}.json.gz"


def read_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def write_cache(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with gzip.open(temp, "wt", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
    temp.replace(path)


def request_headers(url: str, token: str) -> dict[str, str]:
    headers = {"User-Agent": "rq3-codex-independent-evidence-review"}
    if urllib.parse.urlparse(url).hostname == "api.github.com":
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def response_record(url: str, status: int, body: str, error: str) -> dict[str, Any]:
    return {
        "url": url,
        "status": status,
        "body": body,
        "error": error,
        "retrieved_utc": utc_now(),
        "body_sha256": sha256_bytes(body.encode("utf-8")),
    }


def request_once(url: str, token: str) -> tuple[int, str, str]:
    request = urllib.request.Request(url, headers=request_headers(url, token))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body, str(exc)
    except (OSError, TimeoutError) as exc:
        return 0, "", str(exc)


def fetch_with_retry(url: str, token: str, attempts: int) -> dict[str, Any]:
    last = (0, "", "not requested")
    for attempt in range(1, attempts + 1):
        last = request_once(url, token)
        if last[0] != 0:
            break
        if attempt < attempts:
            delay = min(30, 2 ** attempt)
            print(f"request_retry attempt={attempt} sleep_seconds={delay} url={url}")
            time.sleep(delay)
    return response_record(url, *last)


def cached_fetch(url: str, token: str, cache_dir: Path, attempts: int) -> dict[str, Any]:
    target = cache_path(cache_dir, url)
    cached = read_cache(target)
    if cached is not None:
        return {**cached, "cache_hit": True, "cache_path": str(target)}
    result = fetch_with_retry(url, token, attempts)
    if result["status"] != 0:
        write_cache(target, result)
    return {**result, "cache_hit": False, "cache_path": str(target)}


def json_payload(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("status") != 200:
        return {}
    try:
        value = json.loads(response.get("body", ""))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def dependency_value(payload: dict[str, Any], package: str) -> tuple[str, str]:
    groups = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
    for group in groups:
        values = payload.get(group) or {}
        if package in values:
            return str(values[package]), group
    return "", ""


def lockfile_version(payload: dict[str, Any], package: str) -> str:
    packages = payload.get("packages") or {}
    resolved = str((packages.get(f"node_modules/{package}") or {}).get("version", ""))
    if resolved:
        return resolved
    legacy = (payload.get("dependencies") or {}).get(package) or {}
    return str(legacy.get("version", "")) if isinstance(legacy, dict) else ""


def dependency_state(text: str | None, path: str, package: str) -> dict[str, str]:
    if text is None:
        return {"status": "file_missing", "version_spec": "", "resolved_version": ""}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"status": "invalid_json", "version_spec": "", "resolved_version": ""}
    if Path(path).name.lower() == "package.json":
        spec, group = dependency_value(payload, package)
        status = "declared" if spec else "package_not_declared"
        return {"status": status, "version_spec": spec, "resolved_version": "", "group": group}
    resolved = lockfile_version(payload, package)
    status = "resolved" if resolved else "package_not_resolved"
    return {"status": status, "version_spec": "", "resolved_version": resolved}


def npm_publish_time(payload: dict[str, Any], version: str) -> str:
    return str((payload.get("time") or {}).get(version, ""))


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def request_item(case: dict[str, str], kind: str, url: str, path: str = "") -> dict[str, str]:
    return {"case_id": case["case_id"], "kind": kind, "url": url, "path": path}


def case_request_plan(case: dict[str, str]) -> list[dict[str, str]]:
    items = [
        request_item(case, "github_commit", commit_url(case["repo"], case["sha"])),
        request_item(case, "github_repository", repository_url(case["repo"])),
        request_item(case, "npm_packument", npm_url(case["package_name"])),
    ]
    for path in json_list(case["dependency_file_paths_json"]):
        items.append(request_item(case, "github_child_file", raw_url(case["repo"], case["sha"], path), path))
        if case.get("parent_sha"):
            url = raw_url(case["repo"], case["parent_sha"], path)
            items.append(request_item(case, "github_parent_file", url, path))
    return items


def request_plan(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [item for row in rows for item in case_request_plan(row)]


def fetch_unique(plan: list[dict[str, str]], token: str, cache: Path, attempts: int) -> dict[str, dict[str, Any]]:
    responses: dict[str, dict[str, Any]] = {}
    urls = list(dict.fromkeys(item["url"] for item in plan))
    for index, url in enumerate(urls, start=1):
        print(f"official_fetch {index}/{len(urls)} url={url}", flush=True)
        responses[url] = cached_fetch(url, token, cache, attempts)
    return responses


def log_row(item: dict[str, str], response: dict[str, Any]) -> dict[str, Any]:
    keys = ("status", "error", "retrieved_utc", "body_sha256", "cache_hit", "cache_path")
    return {**item, **{key: response.get(key, "") for key in keys}}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def commit_summary(response: dict[str, Any]) -> dict[str, Any]:
    payload = json_payload(response)
    commit = payload.get("commit") or {}
    parents = payload.get("parents") or []
    return {
        "status": response.get("status", 0),
        "author_date": str((commit.get("author") or {}).get("date", "")),
        "committer_date": str((commit.get("committer") or {}).get("date", "")),
        "parent_sha": str((parents[0] if parents else {}).get("sha", "")),
        "parent_count": len(parents),
    }


def repo_summary(response: dict[str, Any]) -> dict[str, Any]:
    payload = json_payload(response)
    return {
        "status": response.get("status", 0),
        "created_at": str(payload.get("created_at", "")),
        "pushed_at": str(payload.get("pushed_at", "")),
    }


def npm_summary(response: dict[str, Any], version: str) -> dict[str, Any]:
    payload = json_payload(response)
    versions = payload.get("versions") or {}
    return {
        "status": response.get("status", 0),
        "version_exists": version in versions,
        "publish_time": npm_publish_time(payload, version),
    }


def states_for_kind(case: dict[str, str], kind: str, responses: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in json_list(case["dependency_file_paths_json"]):
        sha = case["sha"] if kind == "child" else case.get("parent_sha", "")
        if not sha:
            continue
        response = responses[raw_url(case["repo"], sha, path)]
        text = response["body"] if response["status"] == 200 else None
        rows.append({"path": path, "http_status": str(response["status"]), **dependency_state(text, path, case["package_name"])})
    return rows


def state_value(state: dict[str, str]) -> str:
    return state.get("version_spec") or state.get("resolved_version") or ""


def compare_states(parent: dict[str, str], child: dict[str, str]) -> str:
    if parent["status"] == "file_missing" and state_value(child):
        return "package_added_parent_file_missing"
    if not state_value(parent) and state_value(child):
        return "package_added"
    if state_value(parent) and state_value(parent) == state_value(child):
        return "preexisting_same_spec_or_version"
    if state_value(parent) and state_value(child):
        return "version_or_spec_changed"
    return "state_unresolved"


def compare_path_states(parent: list[dict[str, str]], child: list[dict[str, str]]) -> list[dict[str, str]]:
    by_path = {row["path"]: row for row in parent}
    missing = {"status": "file_missing", "version_spec": "", "resolved_version": ""}
    return [{"path": row["path"], "state": compare_states(by_path.get(row["path"], missing), row)} for row in child]


def evidence_complete(commit: dict[str, Any], repo: dict[str, Any], npm: dict[str, Any], child: list[dict[str, str]]) -> bool:
    required = commit["status"] == 200 and repo["status"] == 200 and npm["status"] == 200
    return required and bool(npm["publish_time"]) and all(row["http_status"] == "200" for row in child)


def matrix_row(case: dict[str, str], responses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    commit = commit_summary(responses[commit_url(case["repo"], case["sha"])])
    repo = repo_summary(responses[repository_url(case["repo"])])
    npm = npm_summary(responses[npm_url(case["package_name"])], case["version"])
    child = states_for_kind(case, "child", responses)
    parent = states_for_kind(case, "parent", responses)
    comparisons = compare_path_states(parent, child) if case.get("parent_sha") else []
    return build_matrix_values(case, commit, repo, npm, child, parent, comparisons)


def build_matrix_values(case: dict[str, str], commit: dict[str, Any], repo: dict[str, Any], npm: dict[str, Any], child: list[dict[str, str]], parent: list[dict[str, str]], comparisons: list[dict[str, str]]) -> dict[str, Any]:
    values = {key: case.get(key, "") for key in MATRIX_FIELDS}
    values.update({
        "form_committer_date": case.get("github_committer_date", ""),
        "form_registry_publish_time": case.get("registry_publish_time", ""),
        "fresh_commit_status": commit["status"], "fresh_author_date": commit["author_date"],
        "fresh_committer_date": commit["committer_date"], "fresh_parent_sha": commit["parent_sha"],
        "fresh_parent_count": commit["parent_count"], "fresh_repo_status": repo["status"],
        "fresh_repo_created_at": repo["created_at"], "fresh_repo_pushed_at": repo["pushed_at"],
        "fresh_npm_status": npm["status"], "fresh_version_exists": str(npm["version_exists"]).lower(),
        "fresh_registry_publish_time": npm["publish_time"],
        "fresh_child_file_states_json": json.dumps(child, separators=(",", ":")),
        "fresh_parent_file_states_json": json.dumps(parent, separators=(",", ":")),
        "fresh_parent_child_states_json": json.dumps(comparisons, separators=(",", ":")),
        "fresh_evidence_complete": str(evidence_complete(commit, repo, npm, child)).lower(),
    })
    return values


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MATRIX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def protected_baseline(review_root: Path) -> dict[str, Any]:
    files = {}
    for name in PROTECTED_NAMES:
        path = review_root / name
        files[name] = fingerprint(path)
    return {"captured_utc": utc_now(), "files": files}


def invariant_values(rows: list[dict[str, Any]], logs: list[dict[str, Any]], baseline: dict[str, Any], review_root: Path) -> dict[str, Any]:
    protected_changed = sum(fingerprint(review_root / name)["sha256"] != value["sha256"] for name, value in baseline["files"].items())
    required_failures = sum(row["fresh_evidence_complete"] != "true" for row in rows)
    return {
        "status": "pass" if len(rows) == 38 and required_failures == 0 and protected_changed == 0 else "fail",
        "rows": len(rows), "unique_case_ids": len({row["case_id"] for row in rows}),
        "request_log_rows": len(logs), "transport_failures": sum(row["status"] == 0 for row in logs),
        "required_evidence_failures": required_failures, "protected_input_changes": protected_changed,
        "adjudicated_labels_written": 0,
    }


def output_manifest(args: argparse.Namespace, baseline: dict[str, Any], outputs: list[Path], invariants: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1, "created_utc": utc_now(),
        "input": fingerprint(args.input), "protected_baseline": baseline,
        "outputs": {path.name: fingerprint(path) for path in outputs},
        "counts": invariants,
    }


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    baseline = protected_baseline(args.review_root)
    write_json(args.output_dir / "input_baseline.json", baseline)
    rows = load_rows(args.input)
    plan = request_plan(rows)
    token = load_token(args.github_token_env)
    responses = fetch_unique(plan, token, args.output_dir / "official_request_cache", args.request_attempts)
    logs = [log_row(item, responses[item["url"]]) for item in plan]
    matrix = [matrix_row(row, responses) for row in rows]
    log_path = args.output_dir / "codex_official_evidence_log.jsonl"
    matrix_path = args.output_dir / "official_evidence_matrix.csv"
    write_jsonl(log_path, logs)
    write_csv(matrix_path, matrix)
    invariants = invariant_values(matrix, logs, baseline, args.review_root)
    invariant_path = args.output_dir / "official_evidence_invariants.json"
    write_json(invariant_path, invariants)
    manifest_path = args.output_dir / "official_evidence_manifest.json"
    outputs = [log_path, matrix_path, invariant_path, args.output_dir / "input_baseline.json"]
    write_json(manifest_path, output_manifest(args, baseline, outputs, invariants))
    print(json.dumps(invariants, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

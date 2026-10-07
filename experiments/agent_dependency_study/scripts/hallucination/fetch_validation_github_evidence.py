"""Fetch cached GitHub commit metadata and parent dependency files.

[IN]: case_inventory.csv, local_evidence.csv, existing child manifest cache,
GITHUB_TOKEN, and GitHub commit/raw endpoints.
[OUT]: github_evidence.csv, github_evidence_manifest.json,
github_evidence_invariants.json, github_case_evidence/<case_id>.json, and a
gzip request cache under github_request_cache/.
[POS]: Resumable Phase 2 history-candidate parent/committer evidence. Automatic state comparison
is a review hint and never a final hallucination adjudication.
[SYNC]: If request cache, fields, or state comparison changes, update
scripts/OUTPUTS.md and the active hallucination validation plan.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from build_validation_cases import (
    HISTORY_VALIDATION_LABELS,
    csv_rows,
    fingerprint_file,
)
from cache_storage import read_cache_text, write_gzip_text
from manifest_enrich import (
    DATA_ROOT,
    load_token,
    manifest_cache_target,
    request_text,
)


VALIDATION_ROOT = Path("data_products/rq2_hallucinated_validation_20260721")
DEFAULT_INVENTORY = VALIDATION_ROOT / "case_inventory.csv"
DEFAULT_LOCAL_EVIDENCE = VALIDATION_ROOT / "local_evidence.csv"
DEFAULT_CHILD_CACHE = DATA_ROOT / "cache" / "rq2_hallucinated"
STRONG_LABEL = "version_published_after_author_date"
SCHEMA_VERSION = 1
Requester = Callable[[str, str], tuple[int, str, str]]

OUTPUT_FIELDS = (
    "case_id", "language", "agent", "repo", "sha", "ecosystem",
    "package_name", "version", "author_date", "parent_sha",
    "parent_count", "github_author_date", "github_committer_date",
    "inventory_author_date", "author_date_match", "registry_publish_time",
    "publish_after_author_seconds", "publish_after_committer_seconds",
    "commit_status", "parent_files_total", "parent_files_200",
    "parent_files_404", "parent_files_error", "parent_child_state",
    "parent_child_states_json", "github_evidence_status",
    "evidence_json_path",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--local-evidence", type=Path, default=DEFAULT_LOCAL_EVIDENCE)
    parser.add_argument("--output-dir", type=Path, default=VALIDATION_ROOT)
    parser.add_argument("--child-cache-dir", type=Path, default=DEFAULT_CHILD_CACHE)
    parser.add_argument("--github-token-env", default="GITHUB_TOKEN")
    return parser.parse_args()


def request_cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / digest[:2] / f"{digest}.json"


def cached_request(
    url: str,
    token: str,
    cache_dir: Path,
    requester: Requester = request_text,
) -> dict[str, Any]:
    target = request_cache_path(cache_dir, url)
    cached, actual = read_cache_text(target)
    if cached is not None:
        result = json.loads(cached)
        result["cache_hit"] = True
        result["cache_path"] = str(actual)
        return result
    status, body, error = requester(url, token)
    result = {
        "url": url, "status": status, "body": body, "error": error,
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "cache_hit": False,
    }
    actual = write_gzip_text(target, json.dumps(result, ensure_ascii=False))
    result["cache_path"] = str(actual)
    return result


def parse_commit_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    commit = payload.get("commit") or {}
    parents = payload.get("parents") or []
    verification = commit.get("verification") or {}
    return {
        "parent_sha": str(parents[0].get("sha", "")) if parents else "",
        "parent_count": len(parents),
        "parent_shas": [str(item.get("sha", "")) for item in parents],
        "github_author_date": str((commit.get("author") or {}).get("date", "")),
        "github_committer_date": str((commit.get("committer") or {}).get("date", "")),
        "verification_verified": bool(verification.get("verified", False)),
        "verification_reason": str(verification.get("reason", "")),
        "html_url": str(payload.get("html_url", "")),
    }


def dependency_value(payload: dict[str, Any], package: str) -> tuple[str, str]:
    for group in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        values = payload.get(group) or {}
        if package in values:
            return str(values[package]), group
    return "", ""


def npm_file_state(text: str | None, path: str, package: str) -> dict[str, str]:
    if text is None:
        return {"status": "file_missing", "version_spec": "", "resolved_version": ""}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"status": "invalid_json", "version_spec": "", "resolved_version": ""}
    if Path(path).name.lower() == "package.json":
        value, group = dependency_value(payload, package)
        status = "declared" if value else "package_not_declared"
        return {"status": status, "version_spec": value, "resolved_version": "", "group": group}
    packages = payload.get("packages") or {}
    resolved = str((packages.get(f"node_modules/{package}") or {}).get("version", ""))
    if not resolved:
        resolved = str(((payload.get("dependencies") or {}).get(package) or {}).get("version", ""))
    status = "resolved" if resolved else "package_not_resolved"
    return {"status": status, "version_spec": "", "resolved_version": resolved}


def commit_api_url(repo: str, sha: str) -> str:
    encoded_repo = "/".join(urllib.parse.quote(part) for part in repo.split("/"))
    return f"https://api.github.com/repos/{encoded_repo}/commits/{sha}"


def raw_file_url(repo: str, sha: str, path: str) -> str:
    encoded_repo = "/".join(urllib.parse.quote(part) for part in repo.split("/"))
    encoded_path = "/".join(urllib.parse.quote(part) for part in path.split("/"))
    return f"https://raw.githubusercontent.com/{encoded_repo}/{sha}/{encoded_path}"


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def seconds_between(later: str, earlier: str) -> str:
    if not later or not earlier:
        return ""
    try:
        return str(int((parse_iso(later) - parse_iso(earlier)).total_seconds()))
    except ValueError:
        return ""


def dates_equal(first: str, second: str) -> str:
    if not first or not second:
        return "unknown"
    try:
        return str(parse_iso(first) == parse_iso(second)).lower()
    except ValueError:
        return "unknown"


def child_file_text(cache_dir: Path, case: dict[str, str], path: str) -> tuple[str | None, str]:
    target = manifest_cache_target(cache_dir, case["repo"], case["sha"], path)
    text, actual = read_cache_text(target)
    return text, str(actual or "")


def compare_file_states(parent: dict[str, str], child: dict[str, str]) -> str:
    if parent["status"] == "file_missing" and child["status"] in {"declared", "resolved"}:
        return "package_added_parent_file_missing"
    if parent["status"] in {"package_not_declared", "package_not_resolved"} and child["status"] in {"declared", "resolved"}:
        return "package_added"
    parent_value = parent.get("version_spec") or parent.get("resolved_version")
    child_value = child.get("version_spec") or child.get("resolved_version")
    if parent_value and child_value and parent_value == child_value:
        return "preexisting_same_spec_or_version"
    if parent_value and child_value and parent_value != child_value:
        return "version_or_spec_changed"
    return "state_unresolved"


def fetch_commit(
    case: dict[str, str], token: str, cache_dir: Path, requester: Requester
) -> tuple[dict[str, Any], dict[str, Any]]:
    response = cached_request(
        commit_api_url(case["repo"], case["sha"]), token, cache_dir, requester
    )
    if response["status"] != 200:
        return response, {}
    try:
        return response, parse_commit_metadata(json.loads(response["body"]))
    except json.JSONDecodeError:
        return response, {}


def fetch_parent_file(
    case: dict[str, str],
    parent_sha: str,
    path: str,
    token: str,
    request_cache: Path,
    child_cache: Path,
    requester: Requester,
) -> dict[str, Any]:
    response = cached_request(
        raw_file_url(case["repo"], parent_sha, path), token, request_cache, requester
    )
    parent_text = response["body"] if response["status"] == 200 else None
    child_text, child_path = child_file_text(child_cache, case, path)
    return {
        "path": path,
        "response": {key: response.get(key) for key in (
            "url", "status", "error", "retrieved_utc", "body_sha256",
            "cache_hit", "cache_path",
        )},
        "parent_state": npm_file_state(parent_text, path, case["package_name"]),
        "child_state": npm_file_state(child_text, path, case["package_name"]),
        "child_cache_path": child_path,
    }


def case_bundle(
    case: dict[str, str],
    local: dict[str, str],
    commit_response: dict[str, Any],
    metadata: dict[str, Any],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    states = [compare_file_states(item["parent_state"], item["child_state"]) for item in files]
    publish = local["registry_publish_time"]
    return {
        "schema_version": SCHEMA_VERSION,
        "case": {key: case[key] for key in (
            "case_id", "language", "agent", "repo", "sha", "ecosystem",
            "package_name", "version", "author_date",
        )},
        "commit_response": {key: commit_response.get(key) for key in (
            "url", "status", "error", "retrieved_utc", "body_sha256",
            "cache_hit", "cache_path",
        )},
        "commit_metadata": metadata,
        "registry_publish_time": publish,
        "publish_after_committer_seconds": seconds_between(
            publish, str(metadata.get("github_committer_date", ""))
        ),
        "parent_files": files,
        "parent_child_states": sorted(set(states)),
        "manual_adjudication": None,
    }


def summary_row(bundle: dict[str, Any]) -> dict[str, Any]:
    case = bundle["case"]
    metadata = bundle["commit_metadata"]
    files = bundle["parent_files"]
    states = bundle["parent_child_states"]
    statuses = [item["response"]["status"] for item in files]
    state = states[0] if len(states) == 1 else "mixed_or_multiple"
    if metadata.get("parent_count") == 0:
        state = "root_commit_no_parent"
    return {
        **case,
        "parent_sha": metadata.get("parent_sha", ""),
        "parent_count": metadata.get("parent_count", 0),
        "github_author_date": metadata.get("github_author_date", ""),
        "github_committer_date": metadata.get("github_committer_date", ""),
        "inventory_author_date": case["author_date"],
        "author_date_match": dates_equal(case["author_date"], metadata.get("github_author_date", "")),
        "registry_publish_time": bundle["registry_publish_time"],
        "publish_after_author_seconds": seconds_between(bundle["registry_publish_time"], case["author_date"]),
        "publish_after_committer_seconds": bundle["publish_after_committer_seconds"],
        "commit_status": bundle["commit_response"].get("status", 0),
        "parent_files_total": len(files),
        "parent_files_200": statuses.count(200),
        "parent_files_404": statuses.count(404),
        "parent_files_error": sum(status not in {200, 404} for status in statuses),
        "parent_child_state": state,
        "parent_child_states_json": json.dumps(states, ensure_ascii=False, separators=(",", ":")),
        "github_evidence_status": github_status(bundle),
        "evidence_json_path": f"github_case_evidence/{case['case_id']}.json",
    }


def github_status(bundle: dict[str, Any]) -> str:
    if bundle["commit_response"].get("status") != 200:
        return "commit_request_error"
    if bundle["commit_metadata"].get("parent_count") == 0:
        return "ready_root_commit_manual_review"
    if not bundle["commit_metadata"].get("parent_sha"):
        return "parent_metadata_missing"
    if any(item["response"]["status"] not in {200, 404} for item in bundle["parent_files"]):
        return "parent_file_request_error"
    if any(item["child_state"]["status"] == "file_missing" for item in bundle["parent_files"]):
        return "child_cache_missing"
    return "ready_for_manual_review"


def load_selected_cases(
    inventory_path: Path, local_path: Path
) -> list[tuple[dict[str, str], dict[str, str]]]:
    inventory = {row["case_id"]: row for row in csv_rows(inventory_path)}
    selected = []
    for local in csv_rows(local_path):
        case = inventory.get(local["case_id"])
        if case and case["final_label"] in HISTORY_VALIDATION_LABELS:
            selected.append((case, local))
    return selected


def collect_case_bundle(
    case: dict[str, str],
    local: dict[str, str],
    commits: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]],
    token: str,
    request_cache: Path,
    child_cache: Path,
    requester: Requester,
) -> dict[str, Any]:
    key = (case["repo"], case["sha"])
    if key not in commits:
        commits[key] = fetch_commit(case, token, request_cache, requester)
    response, metadata = commits[key]
    parent_sha = str(metadata.get("parent_sha", ""))
    files = []
    if parent_sha:
        for path in json.loads(case["dep_file_paths_json"]):
            files.append(fetch_parent_file(
                case, parent_sha, path, token, request_cache, child_cache,
                requester,
            ))
    return case_bundle(case, local, response, metadata, files)


def collect_github_evidence(
    inventory_path: Path,
    local_path: Path,
    output_dir: Path,
    child_cache_dir: Path,
    token: str,
    requester: Requester = request_text,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    selected = load_selected_cases(inventory_path, local_path)
    request_cache = output_dir / "github_request_cache"
    commits: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    bundles = {}
    for case, local in selected:
        bundles[case["case_id"]] = collect_case_bundle(
            case, local, commits, token, request_cache, child_cache_dir,
            requester,
        )
    rows = sorted((summary_row(bundle) for bundle in bundles.values()), key=lambda row: row["case_id"])
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "inventory": fingerprint_file(inventory_path),
        "local_evidence": fingerprint_file(local_path),
        "selected_cases": len(rows),
        "unique_commits": len(commits),
    }
    return rows, bundles, manifest


def validate(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    ids = [row["case_id"] for row in rows]
    report = {
        "schema_version": SCHEMA_VERSION,
        "checked_cases": len(rows),
        "duplicate_case_ids": len(ids) - len(set(ids)),
        "selected_count_mismatch": int(len(rows) != manifest["selected_cases"]),
        "commit_request_error_cases": sum(row["commit_status"] != 200 for row in rows),
        "root_commit_cases": sum(row["parent_count"] == 0 for row in rows),
        "parent_metadata_missing_cases": sum(
            row["parent_count"] > 0 and not row["parent_sha"] for row in rows
        ),
        "parent_file_error_cases": sum(row["parent_files_error"] > 0 for row in rows),
        "child_cache_missing_cases": sum(row["github_evidence_status"] == "child_cache_missing" for row in rows),
        "author_date_mismatch_cases": sum(row["author_date_match"] != "true" for row in rows),
        "committer_time_missing_cases": sum(not row["github_committer_date"] for row in rows),
    }
    ignored = {
        "schema_version", "checked_cases", "author_date_mismatch_cases",
        "root_commit_cases",
    }
    report["status"] = "pass" if sum(value for key, value in report.items() if key not in ignored) == 0 else "fail"
    return report


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_outputs(
    output_dir: Path,
    rows: list[dict[str, Any]],
    bundles: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
    invariants: dict[str, Any],
) -> None:
    for case_id, bundle in bundles.items():
        atomic_json(output_dir / "github_case_evidence" / f"{case_id}.json", bundle)
    path = output_dir / "github_evidence.csv"
    atomic_csv(path, rows)
    manifest["output"] = fingerprint_file(path)
    manifest["output"]["rows"] = len(rows)
    atomic_json(output_dir / "github_evidence_manifest.json", manifest)
    atomic_json(output_dir / "github_evidence_invariants.json", invariants)


def main() -> None:
    args = parse_args()
    token = load_token(args.github_token_env)
    if not token:
        raise SystemExit(f"ERROR: {args.github_token_env} not found")
    rows, bundles, manifest = collect_github_evidence(
        args.inventory, args.local_evidence, args.output_dir,
        args.child_cache_dir, token,
    )
    invariants = validate(rows, manifest)
    write_outputs(args.output_dir, rows, bundles, manifest, invariants)
    print(json.dumps({
        "cases": len(rows), "unique_commits": manifest["unique_commits"],
        "invariants": invariants["status"],
    }))
    if invariants["status"] != "pass":
        raise SystemExit(2)


if __name__ == "__main__":
    main()

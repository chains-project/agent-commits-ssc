"""E2: read existing diff/config artifacts only for the frozen probability sample."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import hashlib
from pathlib import Path

from exploratory_observability_common import bounded_line, dump_json, load_json, pseudonym, sha256_file

CONFIG_RE = re.compile(r"(?i)(?:^|/)(?:\.github|\.claude|\.cursor|\.continue|\.aider|\.qwen|\.trae|config|session|agent|provenance|metadata)[^/]*|(?:model|agent|session|provenance).*(?:json|ya?ml|toml)$")
CONTEXT_RE = re.compile(r"(?i)\b(?:model|provider|generated[- ]by|co-authored-by|claude|gpt(?:-?\d|\b)|codex|gemini|qwen|deepseek|kimi|glm[- ]?\d|llama|mistral|gemma|grok|doubao|minimax|ernie|hunyuan|internlm|baichuan)\b")


def reconstruct_targets(sample: list[dict[str, str]], inputs: list[dict], salt: str) -> dict[tuple[str, str], dict[str, str]]:
    by_locator = {(row["source_artifact"], int(row["source_line_number"])): row for row in sample}
    paths: dict[str, list[Path]] = {}
    for row in inputs:
        if row["logical_role"] in {"matched_raw_message", "matched_retry_raw_message"}:
            path = Path(row["resolved_path"]); paths.setdefault(path.name, []).append(path)
    targets = {}
    for (artifact, line_number), sample_row in by_locator.items():
        for path in paths[artifact]:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for current, line in enumerate(handle, 1):
                    if current != line_number:
                        continue
                    envelope = json.loads(line)
                    if pseudonym(str(envelope.get("repo_sha") or ""), salt) != sample_row["repo_sha_key"]:
                        break
                    targets[(str(envelope["repo"]), str(envelope["sha"]))] = sample_row
                    break
            if int(sample_row["selection_rank"]) in {int(row["selection_rank"]) for row in targets.values()}:
                break
    return targets


def index_hits(index_path: Path, targets: dict[tuple[str, str], dict[str, str]]) -> dict[int, dict[str, str]]:
    hits = {}
    with index_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            sample = targets.get((row["repo"], row["sha"]))
            if sample is not None:
                hits[int(sample["selection_rank"])] = row
    return hits


def artifact_context(index: dict[str, str] | None) -> dict[str, str]:
    empty = {"diff_available": "false", "diff_status": "not_in_local_index", "files_count": "", "config_candidate_files": "", "diff_model_context": ""}
    if not index:
        return empty
    result = {"diff_available": str(index["status"] == "ok").lower(), "diff_status": index["status"], "files_count": index["files_count"], "config_candidate_files": "", "diff_model_context": ""}
    json_path, patch_path = Path(index["json_path"]), Path(index["patch_path"])
    if index["status"] == "ok":
        missing = [path.name for path in (json_path, patch_path) if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"Indexed-ok artifacts missing: {', '.join(missing)}")
    if json_path.is_file():
        payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
        names = [bounded_line(row.get("filename", ""), 160) for row in payload.get("files", []) if CONFIG_RE.search(row.get("filename", ""))]
        result["config_candidate_files"] = ";".join(names[:12])[:640]
    if patch_path.is_file():
        lines = []
        with patch_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if CONTEXT_RE.search(line):
                    lines.append(bounded_line(line, 240))
                if len(lines) == 4:
                    break
        result["diff_model_context"] = " || ".join(lines)[:640]
    return result


def run(run_dir: Path) -> dict:
    freeze = load_json(run_dir / "EXPLORATORY_E0_FREEZE.json")
    sample_path = run_dir / "EXPLORATORY_PROBABILITY_SAMPLE.csv"
    with sample_path.open("r", encoding="utf-8", newline="") as handle:
        sample = list(csv.DictReader(handle))
    targets = reconstruct_targets(sample, freeze["inputs"], freeze["pseudonymization_salt"])
    index_path = Path(next(row["resolved_path"] for row in freeze["inputs"] if row["logical_role"] == "patch_commit_index"))
    hits = index_hits(index_path, targets)
    context_rows = []
    for row in sample:
        rank = int(row["selection_rank"])
        context_rows.append({"selection_rank": rank, **artifact_context(hits.get(rank))})
    context_path = run_dir / "EXPLORATORY_TARGETED_DIFF_CONTEXT.csv"
    fields = list(context_rows[0])
    with context_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n"); writer.writeheader(); writer.writerows(context_rows)
    merged_path = run_dir / "EXPLORATORY_AGENT_CODING_PACKAGE.csv"
    merged = [{**row, **{k: v for k, v in context_rows[i].items() if k != "selection_rank"}} for i, row in enumerate(sample)]
    with merged_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(merged[0]), lineterminator="\n"); writer.writeheader(); writer.writerows(merged)
    recode = sorted(merged, key=lambda r: hashlib.sha256(f"{freeze['sampling_seed']}|recode|{r['selection_rank']}".encode()).hexdigest())[:12]
    recode.sort(key=lambda r: int(r["selection_rank"]))
    recode_path = run_dir / "EXPLORATORY_AGENT_RECODE_PACKAGE.csv"
    with recode_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(recode[0]), lineterminator="\n"); writer.writeheader(); writer.writerows(recode)
    result = {"schema_version": "exploratory-targeted-diff-v1", "status": "complete", "sample_targets": len(sample), "targets_reconstructed": len(targets), "index_matches": len(hits), "diff_available": sum(r["diff_available"] == "true" for r in context_rows), "context_sha256": sha256_file(context_path), "coding_package_sha256": sha256_file(merged_path), "recode_package_sha256": sha256_file(recode_path), "recode_rows": 12, "unbounded_diff_scan_performed": False, "full_patch_text_persisted": 0}
    dump_json(run_dir / "EXPLORATORY_TARGETED_DIFF_MANIFEST.json", result)
    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--run-dir", type=Path, required=True); print(run(parser.parse_args().run_dir))

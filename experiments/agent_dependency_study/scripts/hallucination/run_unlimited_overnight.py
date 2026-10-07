"""Run the full three-stage hallucinated-dependency pipeline without sample caps.

[IN]: local commit JSON diff index plus merged agent population consumed by
hallucinated_dependency_probe.py.
[OUT]: fixed-root per-language stage outputs, logs, status.csv, summary.json,
unlimited_interim_report.md, unlimited_experiment_report.md, and shared caches under
THESIS_DATA_ROOT/cache/rq2_hallucinated.
[POS]: Full-size stage-DAG orchestrator for the Hallucinated Dependencies pipeline.
[SYNC]: If output paths or report files change, update scripts/OUTPUTS.md and
.claude/plans/2026-07-07-hallucination-full-pipeline.md.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def raise_csv_field_limit() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


raise_csv_field_limit()

PYTHON = Path(r"C:\ProgramData\Anaconda3\python.exe")
ROOT = Path("data_products/rq2_hallucinated_unlimited_overnight_20260710")
DATA_ROOT = Path(os.environ.get("THESIS_DATA_ROOT", r"D:\MasterThesis\thesis-work-data"))
MANIFEST_CACHE = DATA_ROOT / "cache" / "rq2_hallucinated"
REGISTRY_CACHE = MANIFEST_CACHE / "registry"
LANGUAGES = ["TypeScript", "Python", "JavaScript", "C#", "Rust", "Go", "PHP", "Java", "C++", "Kotlin"]
STOP_REQUESTED = threading.Event()
STATUS_LOCK = threading.Lock()
SUMMARY_LOCK = threading.Lock()
REPORT_LOCK = threading.Lock()
CONSOLE_LOCK = threading.Lock()
MEMORY_ADMISSION_LOCK = threading.Lock()
LAST_MEMORY_ADMISSION = 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--output-root", type=Path, default=ROOT, help="Fixed output root for the unlimited overnight run.")
    parser.add_argument("--languages", nargs="*", default=LANGUAGES, help="Languages to run; default is the full configured language set.")
    parser.add_argument("--stage1-workers", type=int, default=4, help="Local Stage1 import-extraction workers.")
    parser.add_argument("--stage2-workers", type=int, default=1, help="GitHub Stage2 manifest-enrichment workers.")
    parser.add_argument("--stage3-workers", type=int, default=1, help="Registry Stage3 labeling workers.")
    parser.add_argument("--max-workers", type=int, default=0,
                        help="Legacy override; values above 0 replace all three stage worker counts.")
    parser.add_argument("--time-budget-hours", type=float, default=0.0, help="0 means no orchestrator deadline.")
    parser.add_argument("--random-seed", type=int, default=20260710, help="Seed passed to Stage1; retained for reproducible ordering when random sampling is used.")
    parser.add_argument("--manifest-cache-dir", type=Path, default=MANIFEST_CACHE,
                        help="Reuse Stage2 manifest/tree/Python wheel/Maven POM cache from previous runs.")
    parser.add_argument("--registry-cache-dir", type=Path, default=REGISTRY_CACHE,
                        help="Reuse Stage3 registry/deps.dev cache from previous runs.")
    parser.add_argument("--manifest-sleep-seconds", type=float, default=0.1, help="Delay between Stage2 manifest/tree/raw fetch operations.")
    parser.add_argument("--tree-max-files", type=int, default=24,
                        help="Maximum prioritized repository-tree manifests parsed per commit.")
    parser.add_argument("--registry-sleep-seconds", type=float, default=0.15, help="Delay between Stage3 registry/advisory requests.")
    parser.add_argument("--registry-retry-count", type=int, default=2, help="Retry count for transient Stage3 registry failures.")
    parser.add_argument("--registry-retry-sleep-seconds", type=float, default=2.0, help="Sleep between Stage3 registry retries.")
    parser.add_argument("--stage1-checkpoint-every-candidates", type=int, default=500,
                        help="Stage1 writes CSV checkpoints every N selected candidates; 0 disables checkpoint writes.")
    parser.add_argument("--stage2-checkpoint-every-commits", type=int, default=100,
                        help="Stage2 writes manifest/dependency CSV checkpoints every N candidate commits; 0 disables mid-run checkpoints.")
    parser.add_argument("--stage2-checkpoint-every-wheel-packages", type=int, default=50,
                        help="Stage2 flushes the Python wheel import-map cache every N newly fetched package versions; 0 disables mid-run flushes.")
    parser.add_argument("--stage3-checkpoint-every-registry-rows", type=int, default=100,
                        help="Stage3 writes registry CSV checkpoints every N registry rows; 0 disables mid-run checkpoints.")
    parser.add_argument("--stage3-checkpoint-every-osv-batches", type=int, default=5,
                        help="Stage3 writes advisory CSV checkpoints every N OSV batches; 0 disables mid-run checkpoints.")
    parser.add_argument("--runner-heartbeat-seconds", type=int, default=300,
                        help="Append running status rows and refresh the interim report while a subprocess is still running.")
    parser.add_argument("--min-free-memory-gb", type=float, default=1.5,
                        help="Minimum available physical and commit memory required before launching a stage child; 0 disables gating.")
    parser.add_argument("--memory-poll-seconds", type=float, default=15.0,
                        help="Delay between memory admission checks while a stage is waiting.")
    parser.add_argument("--memory-launch-stagger-seconds", type=float, default=5.0,
                        help="Minimum gap between admitted child launches so memory usage becomes observable.")
    parser.add_argument("--quit-key", default="q",
                        help="Single console key that requests a graceful stop; empty string disables the listener.")
    parser.add_argument("--stage1-timeout-minutes", type=int, default=0, help="0 means no subprocess timeout.")
    parser.add_argument("--manifest-timeout-minutes", type=int, default=0, help="0 means no subprocess timeout.")
    parser.add_argument("--registry-timeout-minutes", type=int, default=0, help="0 means no subprocess timeout.")
    return parser.parse_args()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_worker_args(args: argparse.Namespace) -> None:
    legacy = max(0, int(args.max_workers or 0))
    for name in ("stage1_workers", "stage2_workers", "stage3_workers"):
        value = legacy or int(getattr(args, name) or 0)
        setattr(args, name, max(1, value))


class MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("memory_load", ctypes.c_ulong),
        ("total_physical", ctypes.c_ulonglong),
        ("available_physical", ctypes.c_ulonglong),
        ("total_page_file", ctypes.c_ulonglong),
        ("available_page_file", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("available_virtual", ctypes.c_ulonglong),
        ("available_extended_virtual", ctypes.c_ulonglong),
    ]


def available_memory_gb() -> float:
    if os.name != "nt":
        return float("inf")
    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0.0
    available = min(status.available_physical, status.available_page_file)
    return available / (1024 ** 3)


def record_memory_transition(
    args: argparse.Namespace, language: str, stage_name: str,
    status: str, available: float, minimum: float,
) -> None:
    message = f"available_gb={available:.2f}"
    if status == "waiting_memory":
        message += f"; required_gb={minimum:.2f}"
    append_status(args.output_root, {
        "timestamp": now(), "language": language, "stage": stage_name,
        "status": status, "message": message,
    })
    if status == "waiting_memory":
        console_print(f"[{language}/{stage_name}] {status} {message}")


def wait_for_memory_locked(
    args: argparse.Namespace, language: str, stage_name: str, minimum: float,
) -> bool:
    global LAST_MEMORY_ADMISSION
    stagger = max(0.0, float(args.memory_launch_stagger_seconds or 0.0))
    delay = stagger - (time.time() - LAST_MEMORY_ADMISSION)
    if LAST_MEMORY_ADMISSION and delay > 0:
        time.sleep(delay)
    waiting = False
    poll_seconds = max(1.0, float(args.memory_poll_seconds or 1.0))
    while not STOP_REQUESTED.is_set():
        available = available_memory_gb()
        if available >= minimum:
            if waiting:
                record_memory_transition(
                    args, language, stage_name, "memory_available", available, minimum,
                )
            LAST_MEMORY_ADMISSION = time.time()
            return True
        if not waiting:
            waiting = True
            record_memory_transition(
                args, language, stage_name, "waiting_memory", available, minimum,
            )
        time.sleep(poll_seconds)
    return False


def wait_for_memory(args: argparse.Namespace, language: str, stage_name: str) -> bool:
    minimum = max(0.0, float(args.min_free_memory_gb or 0.0))
    if minimum <= 0:
        return True
    with MEMORY_ADMISSION_LOCK:
        return wait_for_memory_locked(args, language, stage_name, minimum)


def console_print(message: str, *, end: str = "\n") -> None:
    with CONSOLE_LOCK:
        print(message, end=end, flush=True)


def safe_name(value: str) -> str:
    special = {"C#": "CSharp", "C++": "Cpp", "F#": "FSharp"}
    if value in special:
        return special[value]
    return "".join(char if char.isalnum() else "_" for char in value).strip("_") or "_"


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        return list(csv.DictReader(line.replace("\x00", "") for line in handle))


def append_status(root: Path, row: dict[str, Any]) -> None:
    path = root / "status.csv"
    fields = [
        "timestamp", "language", "stage", "status", "returncode", "start_utc", "end_utc",
        "duration_seconds", "output_dir", "log_path", "message",
    ]
    with STATUS_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists()
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if not exists:
                writer.writeheader()
            writer.writerow({field: row.get(field, "") for field in fields})


def expected_stage_files(stage1_dir: Path, stage2_dir: Path, stage_name: str) -> list[Path]:
    if stage_name == "stage1_import_extraction":
        return [stage1_dir / "candidate_commits.csv", stage1_dir / "import_events.csv", stage1_dir / "dependency_resolution.csv"]
    if stage_name == "stage2_manifest_enrichment":
        return [stage2_dir / "manifest_fetch_results.csv", stage2_dir / "parsed_dependencies.csv", stage2_dir / "import_dependency_matches.csv"]
    if stage_name == "stage3_registry_label":
        return [stage2_dir / "registry_lookup_results.csv", stage2_dir / "advisory_lookup_results.csv", stage2_dir / "final_labels.csv"]
    return []


def stage_done(stage1_dir: Path, stage2_dir: Path, stage_name: str) -> bool:
    files = expected_stage_files(stage1_dir, stage2_dir, stage_name)
    if not files or not all(path.exists() for path in files):
        return False
    if stage_name == "stage1_import_extraction":
        checkpoint = stage1_dir / "stage1_checkpoint.json"
    elif stage_name == "stage2_manifest_enrichment":
        checkpoint = stage2_dir / "stage2_checkpoint.json"
    elif stage_name == "stage3_registry_label":
        checkpoint = stage2_dir / "stage3_checkpoint.json"
    else:
        checkpoint = None
    if checkpoint is not None:
        if not checkpoint.exists():
            return False
        try:
            return bool(json.loads(checkpoint.read_text(encoding="utf-8", errors="replace")).get("complete"))
        except json.JSONDecodeError:
            return False
    return True


def start_quit_listener(quit_key: str) -> None:
    key = (quit_key or "").lower()
    if not key:
        return
    try:
        import msvcrt
    except ImportError:
        console_print("quit_key_listener_unavailable non_windows_console")
        return
    if not sys.stdin or not sys.stdin.isatty():
        console_print("quit_key_listener_unavailable stdin_not_tty")
        return

    def listen() -> None:
        console_print(f"Press '{key}' to stop after each running stage reaches its next checkpoint.")
        while not STOP_REQUESTED.is_set():
            if msvcrt.kbhit():
                char = msvcrt.getwch().lower()
                if char == key:
                    STOP_REQUESTED.set()
                    console_print(f"\nquit_key_received key={key}; waiting for checkpoint-safe subprocess stops...")
                    break
            time.sleep(0.2)

    threading.Thread(target=listen, name="quit-key-listener", daemon=True).start()


def is_stage_checkpoint_line(line: str) -> bool:
    markers = ("stage1_checkpoint", "stage2_checkpoint", "stage3_checkpoint")
    return any(marker in line for marker in markers)


def terminate_for_quit(proc: subprocess.Popen, log, prefix: str) -> tuple[int, str]:
    log.write("\nquit_key_requested=" + now() + "\n")
    log.flush()
    console_print(prefix + "quit_key_requested; terminating child process")
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)
    log.write("end_utc=" + now() + "\n")
    log.write("returncode=130\n")
    return 130, "stopped_by_quit"


def run_command(cmd: list[str], timeout_minutes: int, log_path: Path, heartbeat=None, heartbeat_seconds: int = 300, display_name: str = "") -> tuple[int, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = now()
    start_time = time.time()
    timeout = None if timeout_minutes <= 0 else timeout_minutes * 60
    heartbeat_seconds = max(0, int(heartbeat_seconds or 0))
    with log_path.open("w", encoding="utf-8", newline="") as log:
        log.write("start_utc=" + start + "\n")
        log.write("command=" + " ".join(cmd) + "\n\n")
        log.flush()
        try:
            child_env = os.environ.copy()
            child_env.setdefault("PYTHONIOENCODING", "utf-8")
            child_env.setdefault("PYTHONUTF8", "1")
            proc = subprocess.Popen(
                cmd,
                cwd=Path.cwd(),
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=child_env,
            )
            output_queue: queue.Queue[str] = queue.Queue()
            reader = threading.Thread(target=enqueue_output, args=(proc.stdout, output_queue), daemon=True)
            reader.start()
            last_heartbeat = time.time()
            prefix = f"[{display_name}] " if display_name else ""
            quit_wait_logged = False
            quit_checkpoint_reached = False
            while proc.poll() is None or not output_queue.empty():
                while not output_queue.empty():
                    line = output_queue.get()
                    log.write(line)
                    log.flush()
                    console_print(prefix + line, end="")
                    if STOP_REQUESTED.is_set() and is_stage_checkpoint_line(line):
                        quit_checkpoint_reached = True
                if STOP_REQUESTED.is_set() and proc.poll() is None:
                    if quit_checkpoint_reached:
                        return terminate_for_quit(proc, log, prefix)
                    if not quit_wait_logged:
                        message = "quit_pending; waiting for next completed stage checkpoint"
                        log.write("\n" + message + "\n")
                        log.flush()
                        console_print(prefix + message)
                        quit_wait_logged = True
                elapsed = time.time() - start_time
                if timeout is not None and elapsed >= timeout and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=30)
                    log.write("\nend_utc=" + now() + "\n")
                    log.write("timeout_minutes=" + str(timeout_minutes) + "\n")
                    console_print(prefix + f"timeout_minutes={timeout_minutes}")
                    return 124, "timeout"
                if heartbeat and heartbeat_seconds and time.time() - last_heartbeat >= heartbeat_seconds:
                    heartbeat()
                    last_heartbeat = time.time()
                time.sleep(0.2)
            returncode = proc.returncode
            log.write("\nend_utc=" + now() + "\n")
            log.write("returncode=" + str(returncode) + "\n")
            console_print(prefix + "returncode=" + str(returncode))
            return returncode, "ok" if returncode == 0 else "command_failed"
        except Exception as exc:
            log.write("\nexception=" + repr(exc) + "\n")
            console_print((f"[{display_name}] " if display_name else "") + "exception=" + repr(exc))
            return 1, "exception"


def enqueue_output(stream, output_queue: queue.Queue[str]) -> None:
    if stream is None:
        return
    for line in iter(stream.readline, ""):
        output_queue.put(line)
    stream.close()

def stage_specs(args: argparse.Namespace, language: str, stage1_dir: Path, stage2_dir: Path) -> list[tuple[str, list[str], int, Path]]:
    return [
        (
            "stage1_import_extraction",
            [
                str(PYTHON), "scripts/hallucination/hallucinated_dependency_probe.py",
                "--languages", language,
                "--sample-per-language", "0",
                "--sampling-mode", "first",
                "--random-seed", str(args.random_seed),
                "--output-dir", str(stage1_dir),
                "--checkpoint-every-candidates", str(args.stage1_checkpoint_every_candidates),
            ],
            args.stage1_timeout_minutes,
            stage1_dir,
        ),
        (
            "stage2_manifest_enrichment",
            [
                str(PYTHON), "scripts/hallucination/manifest_enrich.py",
                "--input-dir", str(stage1_dir),
                "--output-dir", str(stage2_dir),
                "--cache-dir", str(args.manifest_cache_dir),
                "--fetch-tree",
                "--tree-mode", "merge-tree",
                "--tree-max-files", str(args.tree_max_files),
                "--sleep-seconds", str(args.manifest_sleep_seconds),
                "--checkpoint-every-commits", str(args.stage2_checkpoint_every_commits),
                "--checkpoint-every-wheel-packages", str(args.stage2_checkpoint_every_wheel_packages),
            ],
            args.manifest_timeout_minutes,
            stage2_dir,
        ),
        (
            "stage3_registry_label",
            [
                str(PYTHON), "scripts/hallucination/registry_label.py",
                "--input-dir", str(stage2_dir),
                "--output-dir", str(stage2_dir),
                "--cache-dir", str(args.registry_cache_dir),
                "--timeout", "30",
                "--sleep-seconds", str(args.registry_sleep_seconds),
                "--retry-count", str(args.registry_retry_count),
                "--retry-sleep-seconds", str(args.registry_retry_sleep_seconds),
                "--checkpoint-every-registry-rows", str(args.stage3_checkpoint_every_registry_rows),
                "--checkpoint-every-osv-batches", str(args.stage3_checkpoint_every_osv_batches),
            ],
            args.registry_timeout_minutes,
            stage2_dir,
        ),
    ]


def language_paths(args: argparse.Namespace, language: str) -> tuple[Path, Path, Path, Path]:
    run_dir = args.output_root / "wave01_nall" / safe_name(language)
    stage1_dir = run_dir / "stage1"
    stage2_dir = run_dir / "manifest_registry"
    logs = args.output_root / "logs" / "wave01_nall" / safe_name(language)
    return run_dir, stage1_dir, stage2_dir, logs


def load_language_summary(run_dir: Path, language: str) -> dict[str, Any]:
    path = run_dir / "summary.json"
    with SUMMARY_LOCK:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                pass
    return {"language": language, "sample_size": "all", "wave": 1, "run_dir": str(run_dir)}


def stage_outcome(language: str, stage_index: int, success: bool, status: str) -> dict[str, Any]:
    return {"language": language, "stage_index": stage_index, "success": success, "status": status}


def stop_stage(
    run_dir: Path, summary: dict[str, Any], language: str, stage_index: int, final_status: str,
) -> dict[str, Any]:
    summary["final_status"] = final_status
    write_language_summary(run_dir, summary)
    return stage_outcome(language, stage_index, False, final_status)


def complete_language(
    run_dir: Path, stage1_dir: Path, stage2_dir: Path, summary: dict[str, Any],
) -> None:
    summary.update(collect_run_counts(stage1_dir, stage2_dir))
    summary["final_status"] = "ok"
    write_language_summary(run_dir, summary)


def skip_existing_stage(
    args: argparse.Namespace, language: str, stage_index: int, stage_name: str,
    out_dir: Path, run_dir: Path, stage1_dir: Path, stage2_dir: Path,
    summary: dict[str, Any],
) -> dict[str, Any]:
    append_status(args.output_root, {
        "timestamp": now(), "language": language, "stage": stage_name,
        "status": "skipped_existing", "returncode": 0, "output_dir": str(out_dir),
        "message": "complete checkpoint and expected output files already exist",
    })
    summary[stage_name] = {"status": "skipped_existing", "returncode": 0, "duration_seconds": 0}
    if stage_index == 2:
        complete_language(run_dir, stage1_dir, stage2_dir, summary)
    else:
        summary["final_status"] = "in_progress"
        write_language_summary(run_dir, summary)
    return stage_outcome(language, stage_index, True, "skipped_existing")


def make_heartbeat(
    args: argparse.Namespace, language: str, stage_name: str, start: float,
    start_utc: str, out_dir: Path, log_path: Path,
):
    def heartbeat() -> None:
        append_status(args.output_root, {
            "timestamp": now(), "language": language, "stage": stage_name, "status": "running",
            "returncode": "", "start_utc": start_utc, "end_utc": "",
            "duration_seconds": round(time.time() - start, 2), "output_dir": str(out_dir),
            "log_path": str(log_path), "message": "heartbeat",
        })
        write_interim_report(args.output_root, args)

    return heartbeat


def record_stage_result(
    args: argparse.Namespace, language: str, stage_name: str, status: str,
    returncode: int, start: float, start_utc: str, out_dir: Path,
    log_path: Path, summary: dict[str, Any],
) -> None:
    duration = round(time.time() - start, 2)
    end_utc = now()
    append_status(args.output_root, {
        "timestamp": end_utc, "language": language, "stage": stage_name,
        "status": status, "returncode": returncode, "start_utc": start_utc,
        "end_utc": end_utc, "duration_seconds": duration,
        "output_dir": str(out_dir), "log_path": str(log_path),
    })
    summary[stage_name] = {
        "status": status, "returncode": returncode,
        "duration_seconds": duration, "log": str(log_path),
    }


def execute_stage(
    args: argparse.Namespace, language: str, stage_index: int, stage_name: str,
    cmd: list[str], timeout_minutes: int, out_dir: Path, logs: Path,
    run_dir: Path, stage1_dir: Path, stage2_dir: Path, summary: dict[str, Any],
) -> dict[str, Any]:
    start = time.time()
    start_utc = now()
    log_path = logs / f"{stage_name}.log"
    heartbeat = make_heartbeat(args, language, stage_name, start, start_utc, out_dir, log_path)
    returncode, status = run_command(
        cmd, timeout_minutes, log_path, heartbeat=heartbeat,
        heartbeat_seconds=args.runner_heartbeat_seconds,
        display_name=f"{language}/{stage_name}",
    )
    record_stage_result(
        args, language, stage_name, status, returncode, start,
        start_utc, out_dir, log_path, summary,
    )
    if returncode != 0:
        return stop_stage(run_dir, summary, language, stage_index, f"failed_at_{stage_name}")
    if stage_index == 2:
        complete_language(run_dir, stage1_dir, stage2_dir, summary)
    else:
        summary["final_status"] = "in_progress"
        write_language_summary(run_dir, summary)
    return stage_outcome(language, stage_index, True, status)


def run_stage(
    args: argparse.Namespace, language: str, stage_index: int, deadline: float | None,
) -> dict[str, Any]:
    run_dir, stage1_dir, stage2_dir, logs = language_paths(args, language)
    summary = load_language_summary(run_dir, language)
    stage_name, cmd, timeout_minutes, out_dir = stage_specs(
        args, language, stage1_dir, stage2_dir,
    )[stage_index]
    if STOP_REQUESTED.is_set():
        return stop_stage(run_dir, summary, language, stage_index, "stopped_by_quit")
    if stage_done(stage1_dir, stage2_dir, stage_name):
        return skip_existing_stage(
            args, language, stage_index, stage_name, out_dir,
            run_dir, stage1_dir, stage2_dir, summary,
        )
    if deadline and time.time() >= deadline:
        return stop_stage(run_dir, summary, language, stage_index, f"stopped_before_{stage_name}")
    if not wait_for_memory(args, language, stage_name):
        return stop_stage(run_dir, summary, language, stage_index, "stopped_while_waiting_memory")
    return execute_stage(
        args, language, stage_index, stage_name, cmd, timeout_minutes, out_dir,
        logs, run_dir, stage1_dir, stage2_dir, summary,
    )


def submit_stage(
    executors: list[ThreadPoolExecutor], futures: dict[Future, tuple[str, int]],
    args: argparse.Namespace, language: str, stage_index: int, deadline: float | None,
) -> None:
    future = executors[stage_index].submit(run_stage, args, language, stage_index, deadline)
    futures[future] = (language, stage_index)


def resolve_stage_future(
    future: Future, language: str, stage_index: int, root: Path,
) -> dict[str, Any]:
    try:
        return future.result()
    except Exception as exc:
        append_status(root, {
            "timestamp": now(), "language": language, "stage": f"stage{stage_index + 1}",
            "status": "exception", "returncode": 1, "message": repr(exc),
        })
        return stage_outcome(language, stage_index, False, "exception")


def release_stage2_barrier(
    executors: list[ThreadPoolExecutor], futures: dict[Future, tuple[str, int]],
    ready_languages: list[str], args: argparse.Namespace, deadline: float | None,
) -> None:
    for language in ready_languages:
        submit_stage(executors, futures, args, language, 1, deadline)
    ready_languages.clear()


def handle_completed_stage(
    future: Future, language: str, stage_index: int, remaining_stage1: int,
    ready_stage2: list[str], executors: list[ThreadPoolExecutor],
    futures: dict[Future, tuple[str, int]], args: argparse.Namespace,
    deadline: float | None,
) -> int:
    outcome = resolve_stage_future(future, language, stage_index, args.output_root)
    write_interim_report(args.output_root, args)
    if stage_index == 0:
        remaining_stage1 -= 1
        if outcome["success"]:
            ready_stage2.append(language)
        if remaining_stage1 == 0 and not STOP_REQUESTED.is_set():
            release_stage2_barrier(executors, futures, ready_stage2, args, deadline)
    elif outcome["success"] and stage_index < 2:
        submit_stage(executors, futures, args, language, stage_index + 1, deadline)
    return remaining_stage1


def run_stage_dag(args: argparse.Namespace, deadline: float | None) -> None:
    worker_counts = [args.stage1_workers, args.stage2_workers, args.stage3_workers]
    executors = [ThreadPoolExecutor(max_workers=count) for count in worker_counts]
    futures: dict[Future, tuple[str, int]] = {}
    remaining_stage1 = len(args.languages)
    ready_stage2: list[str] = []
    try:
        for language in args.languages:
            submit_stage(executors, futures, args, language, 0, deadline)
        while futures and not STOP_REQUESTED.is_set():
            completed, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
            for future in completed:
                language, stage_index = futures.pop(future)
                remaining_stage1 = handle_completed_stage(
                    future, language, stage_index, remaining_stage1, ready_stage2,
                    executors, futures, args, deadline,
                )
    finally:
        cancel_pending = STOP_REQUESTED.is_set()
        for future in futures:
            if cancel_pending:
                future.cancel()
        for executor in executors:
            executor.shutdown(wait=True, cancel_futures=cancel_pending)


def collect_run_counts(stage1_dir: Path, stage2_dir: Path) -> dict[str, Any]:
    final_rows = read_csv(stage2_dir / "final_labels.csv")
    registry_rows = read_csv(stage2_dir / "registry_lookup_results.csv")
    match_rows = read_csv(stage2_dir / "import_dependency_matches.csv")
    candidate_rows = read_csv(stage1_dir / "candidate_commits.csv")
    import_rows = read_csv(stage1_dir / "import_events.csv")
    labels = Counter(row.get("final_label", "") for row in final_rows)
    reasons = Counter(row.get("cannot_compare_reason", "") for row in final_rows if row.get("cannot_compare_reason"))
    registry_buckets = Counter(row.get("registry_failure_bucket", "") for row in registry_rows)
    return {
        "candidate_commits": len(candidate_rows),
        "import_events": len(import_rows),
        "import_dependency_matches": len(match_rows),
        "registry_lookup_rows": len(registry_rows),
        "final_label_rows": len(final_rows),
        "final_labels": dict(labels),
        "cannot_compare_reasons": dict(reasons),
        "registry_failure_buckets": dict(registry_buckets),
    }


def write_language_summary(run_dir: Path, summary: dict[str, Any]) -> None:
    with SUMMARY_LOCK:
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "summary.json").open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)


def load_summaries(root: Path) -> list[dict[str, Any]]:
    rows = []
    with SUMMARY_LOCK:
        for path in root.glob("wave01_nall/*/summary.json"):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8", errors="replace")))
            except json.JSONDecodeError:
                continue
    return rows


def write_interim_report(root: Path, args: argparse.Namespace) -> None:
    with REPORT_LOCK:
        report = build_report(root, args, False)
        (root / "unlimited_interim_report.md").write_text(report, encoding="utf-8")


def write_final_report(root: Path, args: argparse.Namespace) -> None:
    with REPORT_LOCK:
        report = build_report(root, args, True)
        summaries = load_summaries(root)
        (root / "unlimited_experiment_report.md").write_text(report, encoding="utf-8")
        with (root / "summary.json").open("w", encoding="utf-8") as handle:
            json.dump({"runs": summaries}, handle, ensure_ascii=False, indent=2)


def build_report(root: Path, args: argparse.Namespace, final: bool) -> str:
    summaries = load_summaries(root)
    with STATUS_LOCK:
        status_rows = read_csv(root / "status.csv")
    labels = Counter()
    reasons = Counter()
    buckets = Counter()
    ok_runs = 0
    failed_runs = 0
    for item in summaries:
        if item.get("final_status") == "ok":
            ok_runs += 1
            labels.update(item.get("final_labels", {}))
            reasons.update(item.get("cannot_compare_reasons", {}))
            buckets.update(item.get("registry_failure_buckets", {}))
        else:
            failed_runs += 1
    lines = [
        "# Unlimited Hallucinated Dependencies Overnight Experiment",
        "",
        f"Report type: {'final' if final else 'interim'}",
        f"Output root: `{root}`",
        f"Generated UTC: {now()}",
        "",
        "## Configuration",
        "",
        f"- Manifest cache dir: `{args.manifest_cache_dir}`",
        f"- Registry cache dir: `{args.registry_cache_dir}`",
        f"- Languages: {', '.join(args.languages)}",
        "- Sample size: unlimited per language (`--sample-per-language 0`)",
        f"- Stage1 checkpoint every candidates: {args.stage1_checkpoint_every_candidates}",
        f"- Stage2 checkpoint every commits: {args.stage2_checkpoint_every_commits}",
        f"- Stage2 wheel-cache checkpoint every package versions: {args.stage2_checkpoint_every_wheel_packages}",
        f"- Stage2 maximum prioritized tree manifests per commit: {args.tree_max_files}",
        f"- Stage3 registry checkpoint every rows: {args.stage3_checkpoint_every_registry_rows}",
        f"- Stage3 OSV checkpoint every batches: {args.stage3_checkpoint_every_osv_batches}",
        f"- Runner heartbeat seconds: {args.runner_heartbeat_seconds}",
        f"- Quit key: `{args.quit_key or 'disabled'}`",
        f"- Stage1 workers: {args.stage1_workers}",
        f"- Stage2 workers: {args.stage2_workers}",
        f"- Stage3 workers: {args.stage3_workers}",
        f"- Legacy max-workers override: {args.max_workers or 'disabled'}",
        f"- Minimum free memory before child launch: {args.min_free_memory_gb} GB",
        f"- Memory admission poll seconds: {args.memory_poll_seconds}",
        f"- Time budget hours: {args.time_budget_hours if args.time_budget_hours else 'none'}",
        "- Stage scheduling: all Stage1 tasks reach a terminal state before Stage2 is released; Stage3 may follow completed Stage2 languages.",
        f"- Stage2: `--fetch-tree --tree-mode merge-tree --tree-max-files {args.tree_max_files}`",
        "- Resume behavior: each stage is skipped when its expected output files already exist.",
        "- Rate-limit behavior: Stage2 waits and retries on GitHub 403/429 rate-limit responses.",
        "",
        "## Run Status",
        "",
        f"- Completed language runs: {ok_runs}",
        f"- Failed or partial language runs: {failed_runs}",
        f"- Stage status rows: {len(status_rows)}",
        "",
        "## Final Labels Aggregated Over Completed Runs",
        "",
        "| Label | Rows |",
        "|---|---:|",
    ]
    for label, count in labels.most_common():
        lines.append(f"| `{label or 'blank'}` | {count:,} |")
    lines.extend(["", "## Cannot Compare Reasons", "", "| Reason | Rows |", "|---|---:|"])
    for reason, count in reasons.most_common():
        lines.append(f"| `{reason or 'blank'}` | {count:,} |")
    lines.extend(["", "## Registry Failure Buckets", "", "| Bucket | Rows |", "|---|---:|"])
    for bucket, count in buckets.most_common():
        lines.append(f"| `{bucket or 'blank'}` | {count:,} |")
    lines.extend(["", "## Per-Language Summary", "", "| Language | Status | Candidates | Imports | Matches | Registry | Final Rows |", "|---|---|---:|---:|---:|---:|---:|"])
    for item in sorted(summaries, key=lambda row: row.get("language", "")):
        lines.append(
            f"| `{item.get('language', '')}` | `{item.get('final_status', '')}` | "
            f"{item.get('candidate_commits', 0):,} | {item.get('import_events', 0):,} | "
            f"{item.get('import_dependency_matches', 0):,} | {item.get('registry_lookup_rows', 0):,} | "
            f"{item.get('final_label_rows', 0):,} |"
        )
    lines.extend([
        "",
        "## Notes",
        "",
        "- This run can be safely restarted with the same command; completed stage outputs are reused.",
        "- Full-size runs may be dominated by GitHub tree/raw fetches and registry rate limits.",
        "- `unknown_deleted_or_unpublished_possible` remains weak current-registry evidence, not proof of historical hallucination.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    normalize_worker_args(args)
    args.output_root.mkdir(parents=True, exist_ok=True)
    start_quit_listener(args.quit_key)
    deadline = time.time() + args.time_budget_hours * 3600 if args.time_budget_hours else None
    run_config = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(args).items()
    }
    with (args.output_root / "run_config.json").open("w", encoding="utf-8") as handle:
        json.dump(run_config, handle, ensure_ascii=False, indent=2)
    run_stage_dag(args, deadline)
    write_final_report(args.output_root, args)


if __name__ == "__main__":
    main()

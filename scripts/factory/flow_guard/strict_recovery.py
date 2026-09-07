"""Bounded browser → service → CLI recovery, never a full production verdict.

Only the registered test script supplies this evidence. The environment allowlist
removes inherited credentials/hooks; transport guards are not OS containment.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MARKER = "STRICT_PROCESS_BROWSER_EVIDENCE="
SCOPE = "strict_master_download_browser_recovery"
_IS_WINDOWS = os.name == "nt"
_ENV_KEYS = frozenset({
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
})


def _source_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (p for folder in ("scripts", "tests") for p in (PROJECT_ROOT / folder).rglob("*")
         if p.is_file() and p.suffix in {".py", ".json", ".js", ".html", ".css"}),
        key=lambda p: p.as_posix(),
    ):
        digest.update(path.relative_to(PROJECT_ROOT).as_posix().encode())
        digest.update(b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def validate_evidence(payload: object) -> list[str]:
    """Reject missing or contradictory observations, including claimed coverage."""
    if not isinstance(payload, dict):
        return ["evidence must be an object"]
    errors = []
    for key, expected in {
        "status": "passed", "scope": SCOPE,
        "paid_provider_calls": 0, "external_requests": 0,
        "recognition_accuracy": "not_run", "actual_content_quality": "not_run",
        "final_state": "READY_TO_PUBLISH", "simulated_cost_cents": 7,
        "ui_actions": ["resume_failed_video", "review_final_video"],
        "posts": ["/api/videos/J-strict-master/resume", "/api/videos/J-strict-master/review"],
    }.items():
        if type(payload.get(key)) is not type(expected) or payload[key] != expected:
            errors.append(f"unexpected or missing {key}")
    if payload.get("process_tree_verified", False) is not False:
        errors.append("OS process-tree containment is not proven")
    if payload.get("strict_transport_replay", "not_run") != "not_run":
        errors.append("full strict production journey is not proven")
    media = payload.get("media_http_statuses")
    if not isinstance(media, list) or not media or any(type(code) is not int or code not in {200, 206} for code in media):
        errors.append("successful browser media HTTP observation required")
    workers = payload.get("workers")
    if not isinstance(workers, list) or len(workers) != 2:
        return errors + ["exactly two Worker executions required"]
    pids = []
    launchers = []
    generation_posts = 0
    for worker, phase, count in zip(workers, ("corrupt", "recover"), (4, 9)):
        if not isinstance(worker, dict):
            errors.append("Worker evidence must be an object")
            continue
        for key, expected in {
            "phase": phase, "status": "passed", "violations": 0,
            "http_requests_matched": count, "expected_requests": count,
            "process_tree_verified": False,
            "worker_entrypoint": "cli.py produce-graph --dispatched",
            "job_state": "FAILED" if phase == "corrupt" else "COMPOSED",
        }.items():
            if type(worker.get(key)) is not type(expected) or worker[key] != expected:
                errors.append(f"{phase}: unexpected or missing {key}")
        pid = worker.get("pid")
        launcher = worker.get("launcher_pid")
        parent = worker.get("parent_pid")
        if (type(pid) is not int or pid <= 0 or type(launcher) is not int or launcher <= 0
                or not (pid == launcher or (type(parent) is int and parent == launcher))):
            errors.append(f"{phase}: Worker PID does not match launcher")
        else:
            pids.append(pid)
            launchers.append(launcher)
        if worker.get("error"):
            errors.append(f"{phase}: Worker reported an error")
        requests = worker.get("requests")
        if not isinstance(requests, list) or len(requests) != count:
            errors.append(f"{phase}: request count mismatch")
            continue
        for request in requests:
            if not isinstance(request, dict) or not all(isinstance(request.get(k), str) and request[k] for k in ("method", "route")):
                errors.append(f"{phase}: invalid request observation")
                continue
            if request["method"] == "POST" and request["route"].endswith("/contents/generations/tasks"):
                generation_posts += 1
                if phase == "recover":
                    errors.append("recovery unexpectedly submitted generation")
    if len(set(pids)) != 2 or len(set(launchers)) != 2:
        errors.append("recovery must run in a distinct Worker process")
    if generation_posts != 1:
        errors.append("expected exactly one fixture generation submission across both Workers")
    return errors


def validate_runner_result(result: dict) -> list[str]:
    """A passed wrapper must retain the matching raw observations and code binding."""
    errors = validate_evidence(result.get("browser_evidence"))
    fingerprint = result.get("source_fingerprint")
    if (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or result.get("source_fingerprint_after") != fingerprint):
        errors.append("missing or inconsistent source fingerprint")
    if result.get("errors") != [] or result.get("exit_code") != 0:
        errors.append("runner errors or unsuccessful exit")
    for field, expected in {
        "paid_provider_calls": 0, "external_requests": 0,
        "process_tree_verified": False, "strict_transport_replay": "not_run",
        "recognition_accuracy": "not_run", "actual_content_quality": "not_run",
    }.items():
        if type(result.get(field)) is not type(expected) or result[field] != expected:
            errors.append(f"wrapper claims invalid {field}")
    return errors


def _run_browser_subprocess(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    """Capture one owned process; timeout cleanup targets only its process tree."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if _IS_WINDOWS else 0
    process = subprocess.Popen(
        command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", creationflags=flags,
        start_new_session=not _IS_WINDOWS,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        cleanup_errors = []
        try:
            if _IS_WINDOWS:
                taskkill = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "taskkill.exe")
                cleanup = subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    env=env, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=15, creationflags=flags,
                )
                if cleanup.returncode:
                    detail = (cleanup.stderr or cleanup.stdout or "").strip()[-1000:]
                    raise OSError(f"owned process-tree cleanup exited {cleanup.returncode}: {detail}")
            else:
                # start_new_session makes this owned PID the process-group ID.
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError) as cleanup_error:
            cleanup_errors.append(str(cleanup_error))
            try:
                process.kill()
            except OSError as kill_error:
                cleanup_errors.append(str(kill_error))
        try:
            stdout, stderr = process.communicate(timeout=10)
            error.output, error.stderr = stdout, stderr
        except (OSError, subprocess.SubprocessError) as reap_error:
            cleanup_errors.append(str(reap_error))
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError) as kill_error:
                cleanup_errors.append(str(kill_error))
        error.cleanup_errors = cleanup_errors
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def run_strict_master_recovery(work_root: Path) -> dict[str, object]:
    """Run the registered local-only browser script with bounded time and scope."""
    work_root = Path(work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    result = {
        "status": "failed", "scope": SCOPE, "paid_provider_calls": 0,
        "external_requests": 0, "process_tree_verified": False,
        "strict_transport_replay": "not_run", "recognition_accuracy": "not_run",
        "actual_content_quality": "not_run", "observed_checks": [], "errors": [],
        "runner": "tests/strict_process_browser_check.py", "workers": [],
    }
    try:
        before = _source_fingerprint()
        result["source_fingerprint"] = before
        env = {key: value for key, value in os.environ.items() if key.upper() in _ENV_KEYS}
        env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        completed = _run_browser_subprocess(
            [sys.executable, str(PROJECT_ROOT / result["runner"]), "--work-root", str(work_root)],
            cwd=PROJECT_ROOT, env=env, timeout=360,
        )
        result.update(exit_code=completed.returncode, stdout_tail=completed.stdout[-6000:], stderr_tail=completed.stderr[-3000:])
        if completed.returncode:
            result["errors"].append(f"browser script exited {completed.returncode}")
        markers = [line[len(MARKER):] for line in completed.stdout.splitlines() if line.startswith(MARKER)]
        if len(markers) != 1:
            result["errors"].append("exactly one browser evidence marker required")
        else:
            payload = json.loads(markers[0])
            result["browser_evidence"] = payload
            if isinstance(payload, dict):
                for key in ("paid_provider_calls", "external_requests"):
                    if type(payload.get(key)) is int and payload[key] >= 0:
                        result[key] = payload[key]
                for key in ("ui_actions", "posts", "workers"):
                    if isinstance(payload.get(key), list):
                        result[key] = payload[key]
            result["errors"].extend(validate_evidence(payload))
            if not result["errors"]:
                result.update({key: payload[key] for key in (
                    "ui_actions", "posts", "workers", "final_state", "simulated_cost_cents", "media_http_statuses"
                )})
        after = _source_fingerprint()
        result["source_fingerprint_after"] = after
        if before != after:
            result["errors"].append("source changed during browser replay")
        if not result["errors"]:
            result["status"] = "passed"
            result["observed_checks"] = [
                "browser_http_resume_failed_video", "browser_http_review_final_video",
                "formal_queue_cli_corrupt_then_recover", "distinct_worker_pids",
                "transport_requests_matched=4+9", "recovery_generation_submissions=0",
                "authored_review_to_READY_TO_PUBLISH", f"source_fingerprint={before}",
            ]
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result["errors"].append(f"{type(error).__name__}: {error}")
        if getattr(error, "cleanup_errors", None):
            result["cleanup_errors"] = error.cleanup_errors
    if result["errors"]:
        result["observed_checks"] = ["strict_browser_recovery_failed"]
    (work_root / "strict-master-recovery.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result

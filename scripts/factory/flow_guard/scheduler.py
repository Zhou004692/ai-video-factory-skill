"""Adaptive Flow Guard scheduling without weakening the full release gate."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from typing import Iterator, Sequence


CRITICAL_PATH_PREFIXES = (
    "scripts/factory/flow_guard/",
    "scripts/factory/production_",
    "scripts/factory/services/production_",
    "scripts/factory/management_",
    "scripts/management_ui/",
)
CRITICAL_PATH_NAMES = {
    "scripts/factory/master_dependencies.py",
    "scripts/factory/node_state_machine.py",
    "scripts/factory/recovery_controller.py",
    "scripts/factory/review_candidates.py",
    "scripts/factory/scene_voiceover.py",
    "scripts/factory/state_machine.py",
    "scripts/flow_guard.py",
    "scripts/management_server.py",
}
SOURCE_SUFFIXES = {".py", ".js", ".json", ".html", ".css"}


@dataclass(frozen=True)
class RunDecision:
    suite: str
    mode: str
    reason: str
    include_incidents: bool = True
    full_baseline_at: datetime | None = None


def _normal_path(value: str | Path) -> str:
    return Path(value).as_posix().lstrip("./").lower()


def _is_critical(path: str | Path) -> bool:
    normalized = _normal_path(path)
    return (
        normalized in CRITICAL_PATH_NAMES
        or any(normalized.startswith(prefix) for prefix in CRITICAL_PATH_PREFIXES)
    )


def choose_run(
    changed_paths: Sequence[str | Path],
    *,
    last_full_at: datetime | None,
    now: datetime | None = None,
    full_interval: timedelta = timedelta(hours=24),
) -> RunDecision:
    """Choose fast incident-backed coverage or the complete production gate."""
    now = now or datetime.now(timezone.utc)
    if last_full_at is None:
        return RunDecision("full", "full", "尚无可信的完整回归基线")
    if now - last_full_at >= full_interval:
        hours = round(full_interval.total_seconds() / 3600, 2)
        return RunDecision(
            "full", "full", f"距上次完整回归已达到 {hours:g} 小时",
        )
    critical = sorted(_normal_path(path) for path in changed_paths if _is_critical(path))
    if critical:
        return RunDecision(
            "full", "full", f"关键边界发生变化：{critical[0]}",
        )
    changed = sorted(_normal_path(path) for path in changed_paths)
    if changed:
        return RunDecision(
            "incremental", "incremental", f"普通代码变化：{changed[0]}",
            full_baseline_at=last_full_at,
        )
    return RunDecision(
        "incremental", "incremental", "守护进程启动快速自检",
        full_baseline_at=last_full_at,
    )


def source_snapshot(paths: Sequence[Path]) -> dict[str, str]:
    """Return content hashes keyed by stable absolute source paths."""
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(
                item for item in path.rglob("*")
                if item.is_file() and item.suffix.lower() in SOURCE_SUFFIXES
            )
    result: dict[str, str] = {}
    for path in sorted(set(files), key=lambda item: item.as_posix()):
        result[path.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def changed_source_paths(
    before: dict[str, str], after: dict[str, str], *, root: Path,
) -> tuple[str, ...]:
    changed = {
        path for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    }
    root = Path(root).resolve()
    result: list[str] = []
    for raw_path in sorted(changed):
        path = Path(raw_path)
        try:
            result.append(path.relative_to(root).as_posix())
        except ValueError:
            result.append(path.as_posix())
    return tuple(result)


def candidate_volume_roots() -> tuple[Path, ...]:
    """List local roots that can host disposable regression workspaces."""
    roots: list[Path] = []
    if os.name == "nt":
        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
        for index in range(26):
            if mask & (1 << index):
                root = Path(f"{chr(65 + index)}:\\")
                if root.exists():
                    roots.append(root.resolve())
    else:
        roots.append(Path(tempfile.gettempdir()).resolve())
    temp_root = Path(tempfile.gettempdir()).resolve()
    temp_volume = Path(temp_root.anchor or "/").resolve()
    if temp_volume not in roots:
        roots.insert(0, temp_volume)
    return tuple(roots)


def _free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def resolve_workspace_root(
    *,
    explicit: Path | None = None,
    minimum_free_bytes: int = 4 * 1024**3,
) -> Path:
    """Resolve a disposable root with enough headroom before media generation."""
    configured = explicit or (
        Path(value) if (value := os.environ.get("AI_VIDEO_FLOW_GUARD_TEMP_ROOT"))
        else None
    )
    if configured is not None:
        root = Path(configured).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        free = _free_bytes(root)
        if free < minimum_free_bytes:
            raise RuntimeError(
                "Flow Guard 临时工作区空间不足："
                f"{root} 仅剩 {free / 1024**3:.2f} GB，"
                f"至少需要 {minimum_free_bytes / 1024**3:.2f} GB。"
                "请通过 --temp-root 或 AI_VIDEO_FLOW_GUARD_TEMP_ROOT 指向"
                "空间充足的磁盘。"
            )
        return root

    candidates = tuple(candidate_volume_roots())
    available = [
        (root.resolve(), _free_bytes(root.resolve()))
        for root in candidates
        if root.exists()
    ]
    eligible = [item for item in available if item[1] >= minimum_free_bytes]
    if not eligible:
        detail = ", ".join(
            f"{root}={free / 1024**3:.2f}GB" for root, free in available
        ) or "未发现可用本地磁盘"
        raise RuntimeError(
            "Flow Guard 没有足够的临时空间；"
            f"至少需要 {minimum_free_bytes / 1024**3:.2f} GB（{detail}）。"
            "请设置 AI_VIDEO_FLOW_GUARD_TEMP_ROOT。"
        )
    volume, _ = max(eligible, key=lambda item: item[1])
    root = (volume / "ai-video-flow-guard").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def prune_stale_workspaces(root: Path) -> tuple[Path, ...]:
    """Remove only Flow Guard run directories whose owning process is gone."""
    root = Path(root).resolve()
    removed: list[Path] = []
    if not root.is_dir():
        return ()
    for path in root.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        parts = path.name.split("-", 2)
        if len(parts) != 3 or parts[0] != "run" or not parts[1].isdigit():
            continue
        if process_is_alive(int(parts[1])):
            continue
        resolved = path.resolve()
        if resolved.parent != root:
            continue
        shutil.rmtree(resolved, ignore_errors=True)
        if not resolved.exists():
            removed.append(resolved)
    return tuple(removed)


@contextmanager
def managed_workspace(root: Path) -> Iterator[Path]:
    """Create a PID-owned disposable workspace and reclaim dead predecessors."""
    root = Path(root).resolve()
    prune_stale_workspaces(root)
    with tempfile.TemporaryDirectory(
        prefix=f"run-{os.getpid()}-", dir=root, ignore_cleanup_errors=True,
    ) as directory:
        yield Path(directory).resolve()


def parse_report_full_timestamp(payload: dict[str, object]) -> datetime | None:
    """Read a trustworthy Full timestamp, including pre-adaptive reports."""
    if payload.get("status") != "passed" or payload.get("paid_provider_calls") != 0:
        return None
    execution = payload.get("execution") or {}
    if isinstance(execution, dict) and execution.get("mode") == "incremental":
        raw = execution.get("full_baseline_at")
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(execution, dict) and execution.get("mode") not in {None, "full"}:
        return None
    if not execution and int(payload.get("journey_count") or 0) <= 0:
        return None
    raw = payload.get("generated_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None

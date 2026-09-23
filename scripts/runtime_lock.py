"""Cross-process lock protecting mutable AI Video Factory runtime state."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import BinaryIO, Iterator


class RuntimeStateBusy(RuntimeError):
    """Raised when another process owns the shared runtime-state lock."""


class RuntimeStateLock:
    def __init__(self, path: Path, *, purpose: str) -> None:
        self.path = Path(path).resolve()
        self.purpose = purpose
        self._stream: BinaryIO | None = None

    def acquire(self) -> "RuntimeStateLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, PermissionError) as exc:
            stream.close()
            raise RuntimeStateBusy(
                "AI Video Factory 运行时正在被其他进程使用；"
                f"无法执行{self.purpose}。请先关闭管理服务后重试。"
            ) from exc
        self._stream = stream
        return self

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None

    def __enter__(self) -> "RuntimeStateLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


@contextmanager
def exclusive_runtime_lock(
    runtime_home: Path,
    *,
    purpose: str,
) -> Iterator[RuntimeStateLock]:
    lock = RuntimeStateLock(Path(runtime_home) / ".runtime.lock", purpose=purpose)
    with lock:
        yield lock

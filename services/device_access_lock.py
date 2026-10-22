"""Cross-process exclusive lock for SSH tunnel and ADB device access."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import BinaryIO, Callable


class DeviceAccessBusyError(TimeoutError):
    """Raised when another resident process keeps the device lock too long."""


class DeviceAccessLock:
    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 0.1,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = Path(path)
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.poll_seconds = max(0.001, float(poll_seconds))
        self.monotonic = monotonic
        self.sleeper = sleeper
        self._handle: BinaryIO | None = None
        self._locked = False

    def __enter__(self) -> "DeviceAccessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.release()
        return False

    def acquire(self) -> None:
        if self._locked:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = self.monotonic() + self.timeout_seconds
        while True:
            try:
                self._try_lock(handle)
            except (BlockingIOError, OSError):
                if self.monotonic() >= deadline:
                    handle.close()
                    raise DeviceAccessBusyError(
                        f"等待设备资源超时：{self.path}"
                    )
                self.sleeper(
                    min(self.poll_seconds, max(0.0, deadline - self.monotonic()))
                )
                continue
            self._handle = handle
            self._locked = True
            return

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if self._locked:
                self._unlock(handle)
        finally:
            self._locked = False
            self._handle = None
            handle.close()

    @staticmethod
    def _try_lock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

"""Bounded, ownership-scoped process tree termination helpers."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable


def _valid_pid(pid: int) -> bool:
    try:
        return int(pid) > 0
    except (TypeError, ValueError):
        return False


def is_process_alive(pid: int, *, platform_name: str | None = None) -> bool:
    """Return whether ``pid`` is currently addressable by this OS."""
    if not _valid_pid(pid):
        return False
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False
    return True


def terminate_process_tree(
    pid: int,
    *,
    grace_seconds: float = 1.0,
    runner: Callable = subprocess.run,
    platform_name: str | None = None,
    sleeper: Callable = time.sleep,
) -> bool:
    """Terminate a process and descendants without touching unrelated PIDs.

    Windows delegates descendant traversal to ``taskkill``.  POSIX workers are
    expected to create a private process group, allowing ``killpg`` to cover
    command children as well.
    """
    if not _valid_pid(pid):
        return False
    normalized_platform = platform_name or sys.platform
    process_id = int(pid)
    grace = max(0.0, float(grace_seconds))

    if normalized_platform == "win32":
        try:
            result = runner(
                ["taskkill", "/PID", str(process_id), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1.0, grace + 1.0),
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return False
        return getattr(result, "returncode", 1) == 0

    try:
        os.killpg(process_id, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        try:
            os.kill(process_id, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return False

    deadline = time.monotonic() + grace
    while is_process_alive(process_id, platform_name=normalized_platform):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sleeper(min(0.05, remaining))

    if is_process_alive(process_id, platform_name=normalized_platform):
        try:
            os.killpg(process_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(process_id, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    return True

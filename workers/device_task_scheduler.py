"""FIFO resource scheduler for device-scoped UI jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class _ScheduledTask:
    task_id: int
    resources: frozenset[str]
    start: Callable[[], None]


def device_resource_key(device: object) -> str:
    """Return the stable physical endpoint key used for device exclusion."""
    public_ip = str(getattr(device, "public_ip", "")).strip()
    remote_ip = str(getattr(device, "remote_ip", "")).strip()
    remote_port = int(getattr(device, "remote_port"))
    return f"device:{public_ip}|{remote_ip}|{remote_port}"


class DeviceTaskScheduler:
    """Start jobs when all resources are free while preserving per-resource FIFO."""

    def __init__(self) -> None:
        self._next_task_id = 1
        self._pending: list[_ScheduledTask] = []
        self._active: dict[int, _ScheduledTask] = {}
        self._resource_owners: dict[str, int] = {}
        self._draining = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def active_task_ids(self) -> tuple[int, ...]:
        return tuple(self._active)

    @property
    def pending_task_ids(self) -> tuple[int, ...]:
        return tuple(task.task_id for task in self._pending)

    @property
    def busy_resources(self) -> frozenset[str]:
        return frozenset(self._resource_owners)

    def submit(self, resources: Iterable[str], start: Callable[[], None]) -> int:
        normalized = frozenset(
            str(resource).strip()
            for resource in resources
            if str(resource).strip()
        )
        if not normalized:
            raise ValueError("a scheduled task must own at least one resource")
        if not callable(start):
            raise TypeError("start must be callable")

        task_id = self._next_task_id
        self._next_task_id += 1
        self._pending.append(_ScheduledTask(task_id, normalized, start))
        self._drain()
        return task_id

    def complete(self, task_id: int) -> bool:
        task = self._active.pop(int(task_id), None)
        if task is None:
            return False
        for resource in task.resources:
            if self._resource_owners.get(resource) == task.task_id:
                self._resource_owners.pop(resource, None)
        self._drain()
        return True

    def cancel_pending(self) -> list[int]:
        cancelled = [task.task_id for task in self._pending]
        self._pending.clear()
        return cancelled

    def clear(self) -> None:
        self._pending.clear()
        self._active.clear()
        self._resource_owners.clear()

    def _drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        try:
            while True:
                ready: list[_ScheduledTask] = []
                remaining: list[_ScheduledTask] = []
                reserved_by_earlier_pending: set[str] = set()
                busy = set(self._resource_owners)

                for task in self._pending:
                    if task.resources & (busy | reserved_by_earlier_pending):
                        remaining.append(task)
                        reserved_by_earlier_pending.update(task.resources)
                        continue
                    ready.append(task)
                    busy.update(task.resources)

                if not ready:
                    self._pending = remaining
                    return

                self._pending = remaining
                for task in ready:
                    self._active[task.task_id] = task
                    for resource in task.resources:
                        self._resource_owners[resource] = task.task_id

                for task in ready:
                    try:
                        task.start()
                    except Exception:
                        self.complete(task.task_id)
                        raise
        finally:
            self._draining = False

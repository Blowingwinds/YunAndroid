"""Qt background runner for blocking device operations."""

from __future__ import annotations

import importlib
import multiprocessing
import os
from dataclasses import dataclass, field
from threading import Event
from typing import Any, Callable, Mapping

from PySide6.QtCore import QObject, QRunnable, Signal

from workers.process_control import terminate_process_tree


@dataclass(frozen=True)
class ProcessTaskSpec:
    """Spawn-safe reference to a top-level callable and its arguments."""

    target: str
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def resolve(self) -> Callable[..., Any]:
        module_name, separator, attribute = self.target.partition(":")
        if not separator or not module_name or not attribute:
            raise ValueError(f"invalid process task target: {self.target}")
        module = importlib.import_module(module_name)
        target = getattr(module, attribute, None)
        if not callable(target):
            raise TypeError(f"process task target is not callable: {self.target}")
        return target


def _run_process_task(spec: ProcessTaskSpec, result_pipe) -> None:
    """Execute one task without importing Qt or touching parent state."""
    if os.name != "nt":
        try:
            os.setsid()
        except OSError:
            pass
    try:
        result = spec.resolve()(*spec.args, **dict(spec.kwargs))
        result_pipe.send(("success", result))
    except Exception as exc:
        result_pipe.send(("failure", f"{type(exc).__name__}: {exc}"))
    finally:
        result_pipe.close()


class TaskWorkerSignals(QObject):
    started = Signal()
    process_started = Signal(int)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class TaskWorker(QRunnable):
    def __init__(
        self,
        callback: Callable[[], object],
        *,
        process_spec: ProcessTaskSpec | None = None,
    ):
        super().__init__()
        self.callback = callback
        self.process_spec = process_spec
        self.signals = TaskWorkerSignals()
        self._cancel_event = Event()
        self._child_process = None
        self._child_pid = None

    def request_cancel(self) -> None:
        """Cancel queued work and terminate a running child process tree."""
        self._cancel_event.set()
        self._terminate_child_process()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def child_pid(self) -> int | None:
        return self._child_pid

    def run(self):
        if self.cancel_requested:
            self.signals.finished.emit()
            return
        self.signals.started.emit()
        try:
            if self.process_spec is None:
                result = self.callback()
            else:
                result = self._run_in_child_process()
            if not self.cancel_requested:
                self.signals.succeeded.emit(result)
        except Exception as exc:
            if not self.cancel_requested:
                self.signals.failed.emit(str(exc))
        finally:
            self._child_process = None
            self._child_pid = None
            self.signals.finished.emit()

    def _run_in_child_process(self):
        context = multiprocessing.get_context("spawn")
        parent_pipe, child_pipe = context.Pipe(duplex=False)
        process = context.Process(target=_run_process_task, args=(self.process_spec, child_pipe))
        self._child_process = process
        try:
            process.start()
        except Exception:
            parent_pipe.close()
            child_pipe.close()
            self._child_process = None
            raise
        child_pipe.close()
        self._child_pid = process.pid
        if self._child_pid:
            self.signals.process_started.emit(int(self._child_pid))

        message = None
        try:
            while process.is_alive():
                if self.cancel_requested:
                    self._terminate_child_process()
                    break
                if parent_pipe.poll(0.05):
                    message = parent_pipe.recv()
                    break
            process.join(timeout=1)
            if process.is_alive():
                self._terminate_child_process()
                process.join(timeout=1)
            if self.cancel_requested:
                return None
            if message is None and parent_pipe.poll():
                message = parent_pipe.recv()
            if not message:
                if process.exitcode:
                    raise RuntimeError(f"child process exited with code {process.exitcode}")
                raise RuntimeError("child process returned no result")
            status, payload = message
            if status == "failure":
                raise RuntimeError(payload)
            return payload
        finally:
            parent_pipe.close()

    def _terminate_child_process(self) -> None:
        """Stop the owned child tree, then fall back to direct process control."""
        child_pid = self._child_pid
        if child_pid:
            terminate_process_tree(child_pid)

        process = self._child_process
        if process is None:
            return
        is_alive = getattr(process, "is_alive", None)
        try:
            alive = bool(is_alive()) if callable(is_alive) else True
        except Exception:
            alive = True
        if not alive:
            return

        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            try:
                terminate()
            except Exception:
                pass
        join = getattr(process, "join", None)
        if callable(join):
            try:
                join(timeout=1)
            except Exception:
                pass

        try:
            alive = bool(is_alive()) if callable(is_alive) else False
        except Exception:
            alive = False
        if not alive:
            return
        kill = getattr(process, "kill", None)
        if callable(kill):
            try:
                kill()
            except Exception:
                return
            if callable(join):
                try:
                    join(timeout=1)
                except Exception:
                    pass

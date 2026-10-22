"""Resident daily-JSON monitor with a loopback control API."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.device_access_lock import DeviceAccessLock
from services.monitor_runner import MonitorRunner
from services.monitoring import BEIJING_TZ, DEFAULT_CONFIG_PATH, MonitorConfig, load_monitor_config


LOG_PATH = PROJECT_ROOT / "data" / "logs" / "json_monitor.log"


def configure_logging(path: str | Path = LOG_PATH) -> logging.Logger:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("sfmanager.json_monitor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


class MonitorDaemon:
    def __init__(
        self,
        config: MonitorConfig,
        *,
        runner: MonitorRunner | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
        device_lock_factory: Callable[[], object] | None = None,
    ):
        if not config.control_token:
            raise ValueError("配置项 [monitor] control_token 不能为空")
        self.config = config
        self.runner = runner or MonitorRunner(config)
        self.monotonic = monotonic
        self.logger = logger or configure_logging()
        self.device_lock_factory = device_lock_factory or (
            lambda: DeviceAccessLock(
                config.state_db.parent / "device_access.lock",
                timeout_seconds=300,
            )
        )
        self.poll_interval_seconds = config.poll_interval_seconds
        self._stop_event = Event()
        self._run_now_event = Event()
        self._state_lock = Lock()
        self._server: ThreadingHTTPServer | None = None
        self._scheduler_thread: Thread | None = None
        self._server_shutdown_requested = False
        self._running = False
        self._status = "starting"
        self._last_summary: dict[str, object] | None = None
        self._last_error = ""
        self._next_run_at = ""

    def create_server(self) -> ThreadingHTTPServer:
        daemon = self

        class ControlHandler(BaseHTTPRequestHandler):
            server_version = "SFManagerMonitor/1.0"

            def log_message(self, format, *args):
                return

            def _send_json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                supplied = self.headers.get("X-Monitor-Token", "")
                return hmac.compare_digest(supplied, daemon.config.control_token)

            def do_GET(self):
                if urlsplit(self.path).path != "/health":
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(200, daemon.health())

            def do_POST(self):
                if not self._authorized():
                    self._send_json(403, {"error": "forbidden"})
                    return
                path = urlsplit(self.path).path
                if path == "/run-now":
                    daemon.request_run_now()
                    self._send_json(202, {"accepted": True})
                    return
                if path == "/shutdown":
                    self._send_json(202, {"accepted": True})
                    daemon.request_shutdown()
                    return
                self._send_json(404, {"error": "not found"})

        server = ThreadingHTTPServer(
            (self.config.control_host, self.config.control_port),
            ControlHandler,
        )
        server.daemon_threads = True
        return server

    def health(self) -> dict[str, object]:
        with self._state_lock:
            return {
                "running": self._running,
                "status": self._status,
                "pid": __import__("os").getpid(),
                "poll_interval_seconds": self.poll_interval_seconds,
                "stale_threshold_seconds": self.config.stale_threshold_seconds,
                "last_summary": self._last_summary,
                "last_error": self._last_error,
                "next_run_at": self._next_run_at,
            }

    def request_run_now(self) -> None:
        self._run_now_event.set()

    def request_shutdown(self) -> None:
        self._stop_event.set()
        self._run_now_event.set()
        server = None
        with self._state_lock:
            self._status = "stopping"
            if self._server is not None and not self._server_shutdown_requested:
                self._server_shutdown_requested = True
                server = self._server
        if server is not None:
            Thread(
                target=server.shutdown,
                name="json-monitor-shutdown",
                daemon=True,
            ).start()

    def _execute_once(self) -> None:
        with self._state_lock:
            self._status = "checking"
            self._last_error = ""
        try:
            with self.device_lock_factory():
                summary = self.runner.run_once()
        except Exception as exc:
            self.logger.exception("监控巡检失败")
            with self._state_lock:
                self._last_error = str(exc)
        else:
            self.logger.info(
                "巡检完成：total=%s normal=%s active_alerts=%s new_alerts=%s",
                summary.get("total"),
                summary.get("normal"),
                summary.get("active_alerts"),
                summary.get("new_alerts"),
            )
            with self._state_lock:
                self._last_summary = summary
        finally:
            with self._state_lock:
                if not self._stop_event.is_set():
                    self._status = "idle"

    def _scheduler_loop(self) -> None:
        next_deadline = self.monotonic()
        while not self._stop_event.is_set():
            timeout = max(0.0, next_deadline - self.monotonic())
            requested = self._run_now_event.wait(timeout)
            if self._stop_event.is_set():
                break
            if requested:
                self._run_now_event.clear()

            self._execute_once()
            current = self.monotonic()
            if not requested or current >= next_deadline:
                while next_deadline <= current:
                    next_deadline += self.poll_interval_seconds
            remaining = max(0.0, next_deadline - self.monotonic())
            with self._state_lock:
                self._next_run_at = (
                    datetime.now(BEIJING_TZ) + timedelta(seconds=remaining)
                ).isoformat()

    def serve(self, server: ThreadingHTTPServer | None = None) -> None:
        self._server = server or self.create_server()
        with self._state_lock:
            self._running = True
            self._status = "idle"
        self._scheduler_thread = Thread(
            target=self._scheduler_loop,
            name="json-monitor-scheduler",
            daemon=False,
        )
        self._scheduler_thread.start()
        address, port = self._server.server_address[:2]
        self.logger.info("监控进程启动：http://%s:%s", address, port)
        try:
            self._server.serve_forever(poll_interval=0.25)
        finally:
            self.request_shutdown()
            self._server.server_close()
            if self._scheduler_thread is not None:
                self._scheduler_thread.join()
            with self._state_lock:
                self._running = False
                self._status = "stopped"
            self.logger.info("监控进程已停止")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SFManager JSON monitor")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--once", action="store_true", help="run one patrol without the control server")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = configure_logging()
    try:
        config = load_monitor_config(args.config)
        runner = MonitorRunner(config, logger=logger.error)
        if args.once:
            summary = runner.run_once()
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        daemon = MonitorDaemon(config, runner=runner, logger=logger)
        signal.signal(signal.SIGINT, lambda _signum, _frame: daemon.request_shutdown())
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda _signum, _frame: daemon.request_shutdown())
        daemon.serve()
        return 0
    except Exception:
        logger.exception("监控进程启动或运行失败")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


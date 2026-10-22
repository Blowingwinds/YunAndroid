"""Resident Excel summary scheduler with a loopback control API."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import signal
import sys
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
from services.excel_summary import (
    BEIJING_TZ,
    DEFAULT_CONFIG_PATH,
    ExcelSummaryConfig,
    ExcelSummaryRunner,
    load_excel_summary_config,
)


LOG_PATH = PROJECT_ROOT / "data" / "logs" / "excel_summary.log"


def configure_logging(path: str | Path = LOG_PATH) -> logging.Logger:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("sfmanager.excel_summary")
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


def next_run_at(now: datetime, hour: int, minute: int) -> datetime:
    moment = now.astimezone(BEIJING_TZ)
    candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < moment:
        candidate += timedelta(days=1)
    return candidate


def initial_run_at(
    now: datetime,
    hour: int,
    minute: int,
    catchup_seconds: int,
    last_completed_run_date: str | None,
) -> datetime:
    moment = now.astimezone(BEIJING_TZ)
    scheduled = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if (
        last_completed_run_date == moment.date().isoformat()
        and moment >= scheduled
    ):
        return scheduled + timedelta(days=1)
    delay = (moment - scheduled).total_seconds()
    if 0 <= delay <= max(0, int(catchup_seconds)):
        return moment
    return next_run_at(moment, hour, minute)


class ExcelSummaryDaemon:
    def __init__(
        self,
        config: ExcelSummaryConfig,
        *,
        runner: ExcelSummaryRunner | None = None,
        logger: logging.Logger | None = None,
        now_provider: Callable[[], datetime] | None = None,
        device_lock_factory: Callable[[], object] | None = None,
    ) -> None:
        if not config.control_token:
            raise ValueError("配置项 [excel_summary_control] control_token 不能为空")
        self.config = config
        self.runner = runner or ExcelSummaryRunner(config)
        self.logger = logger or configure_logging()
        self.now_provider = now_provider or (lambda: datetime.now(BEIJING_TZ))
        self.device_lock_factory = device_lock_factory or (
            lambda: DeviceAccessLock(
                config.state_file.parent / "device_access.lock",
                timeout_seconds=config.device_lock_timeout_seconds,
            )
        )
        self._stop_event = Event()
        self._run_now_event = Event()
        self._state_lock = Lock()
        self._server: ThreadingHTTPServer | None = None
        self._scheduler_thread: Thread | None = None
        self._server_shutdown_requested = False
        self._running = False
        self._status = "starting"
        self._last_summary = self._load_state()
        self._last_error = ""
        self._next_run_at = ""

    def _load_state(self) -> dict[str, object] | None:
        try:
            payload = json.loads(self.config.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def create_server(self) -> ThreadingHTTPServer:
        daemon = self

        class ControlHandler(BaseHTTPRequestHandler):
            server_version = "SFManagerExcelSummary/1.0"

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
                    accepted = daemon.request_run_now()
                    self._send_json(202 if accepted else 409, {"accepted": accepted})
                    return
                if path == "/shutdown":
                    self._send_json(202, {"accepted": True})
                    daemon.request_shutdown()
                    return
                self._send_json(404, {"error": "not found"})

        server = ThreadingHTTPServer(
            (self.config.control_host, self.config.control_port), ControlHandler
        )
        server.daemon_threads = True
        return server

    def health(self) -> dict[str, object]:
        with self._state_lock:
            return {
                "running": self._running,
                "status": self._status,
                "pid": __import__("os").getpid(),
                "run_time": self.config.run_time,
                "source_date": self.config.source_date,
                "tag": self.config.tag,
                "max_workers": self.config.max_workers,
                "last_summary": self._last_summary,
                "last_error": self._last_error,
                "next_run_at": self._next_run_at,
            }

    def request_run_now(self) -> bool:
        with self._state_lock:
            if self._status in {"waiting_for_devices", "summarizing", "stopping"}:
                return False
            if self._run_now_event.is_set():
                return False
            self._run_now_event.set()
            return True

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
                name="excel-summary-shutdown",
                daemon=True,
            ).start()

    def _execute_once(self) -> None:
        with self._state_lock:
            self._status = "waiting_for_devices"
            self._last_error = ""
        try:
            with self.device_lock_factory():
                with self._state_lock:
                    self._status = "summarizing"
                summary = self.runner.run_once()
        except Exception as exc:
            self.logger.exception("Excel 日报汇总失败")
            with self._state_lock:
                self._last_error = str(exc)
        else:
            self.logger.info(
                "汇总完成：output=%s rows=%s success=%s failed=%s",
                summary.get("output_file"),
                summary.get("row_count"),
                summary.get("success"),
                summary.get("failed"),
            )
            notification_error = str(summary.get("notification_error") or "")
            if notification_error:
                self.logger.warning(
                    "飞书汇总通知发送失败：%s",
                    notification_error,
                )
            with self._state_lock:
                self._last_summary = summary
        finally:
            with self._state_lock:
                if not self._stop_event.is_set():
                    self._status = "idle"

    def _scheduler_loop(self) -> None:
        hour, minute = self.config.run_clock
        now = self.now_provider().astimezone(BEIJING_TZ)
        last_run_date = None
        if isinstance(self._last_summary, dict):
            last_run_date = str(self._last_summary.get("run_date") or "") or None
        target = initial_run_at(
            now,
            hour,
            minute,
            self.config.catchup_seconds,
            last_run_date,
        )
        while not self._stop_event.is_set():
            with self._state_lock:
                self._next_run_at = target.isoformat()
            current = self.now_provider().astimezone(BEIJING_TZ)
            timeout = max(0.0, (target - current).total_seconds())
            requested = self._run_now_event.wait(timeout)
            if self._stop_event.is_set():
                break
            if requested:
                self._run_now_event.clear()

            self._execute_once()
            current = self.now_provider().astimezone(BEIJING_TZ)
            if not requested or current >= target:
                target = next_run_at(current + timedelta(microseconds=1), hour, minute)

    def serve(self, server: ThreadingHTTPServer | None = None) -> None:
        self._server = server or self.create_server()
        with self._state_lock:
            self._running = True
            self._status = "idle"
        self._scheduler_thread = Thread(
            target=self._scheduler_loop,
            name="excel-summary-scheduler",
            daemon=False,
        )
        self._scheduler_thread.start()
        address, port = self._server.server_address[:2]
        self.logger.info("Excel 汇总进程启动：http://%s:%s", address, port)
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
            self.logger.info("Excel 汇总进程已停止")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SFManager Excel summary daemon")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--once", action="store_true", help="run one summary without the control server")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = configure_logging()
    try:
        config = load_excel_summary_config(args.config)
        runner = ExcelSummaryRunner(config)
        if args.once:
            with DeviceAccessLock(
                config.state_file.parent / "device_access.lock",
                timeout_seconds=config.device_lock_timeout_seconds,
            ):
                summary = runner.run_once()
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        daemon = ExcelSummaryDaemon(config, runner=runner, logger=logger)
        signal.signal(signal.SIGINT, lambda _signum, _frame: daemon.request_shutdown())
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda _signum, _frame: daemon.request_shutdown())
        daemon.serve()
        return 0
    except Exception:
        logger.exception("Excel 汇总进程启动或运行失败")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

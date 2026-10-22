"""Desktop-side controller for the detached JSON monitor process."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from services.feishu_notifier import FeishuNotifier
from services.monitoring import DEFAULT_CONFIG_PATH, MonitorConfig, load_monitor_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MONITOR_SCRIPT = PROJECT_ROOT / "tools" / "json_monitor.py"


class MonitorController:
    def __init__(
        self,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        *,
        opener: Callable = urlopen,
        popen: Callable = subprocess.Popen,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        platform_name: str | None = None,
    ):
        self.config_path = Path(config_path).resolve()
        self.opener = opener
        self.popen = popen
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.platform_name = platform_name or sys.platform

    def _config(self) -> MonitorConfig:
        return load_monitor_config(self.config_path)

    @staticmethod
    def _base_url(config: MonitorConfig) -> str:
        return f"http://{config.control_host}:{config.control_port}"

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        config: MonitorConfig | None = None,
        timeout: float = 1.0,
    ) -> dict:
        current = config or self._config()
        headers = {"Accept": "application/json"}
        data = None
        if method != "GET":
            headers.update(
                {
                    "Content-Type": "application/json",
                    "X-Monitor-Token": current.control_token,
                }
            )
            data = b"{}"
        request = Request(
            f"{self._base_url(current)}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        with self.opener(request, timeout=timeout) as response:
            payload = response.read()
        try:
            result = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("监控进程返回了无效响应") from exc
        if not isinstance(result, dict):
            raise RuntimeError("监控进程返回格式错误")
        return result

    def status(self) -> dict:
        try:
            config = self._config()
            return self._request("/health", config=config)
        except (FileNotFoundError, ValueError) as exc:
            return {"running": False, "status": "config_error", "error": str(exc)}
        except (HTTPError, URLError, OSError, TimeoutError):
            return {"running": False, "status": "stopped", "error": ""}

    def start(self, *, startup_timeout: float = 5.0) -> dict:
        config = self._config()
        if not config.control_token:
            raise ValueError("配置项 [monitor] control_token 不能为空")
        FeishuNotifier(config.feishu_webhook_url, secret=config.feishu_secret)
        current = self.status()
        if current.get("running"):
            return current
        if not MONITOR_SCRIPT.is_file():
            raise FileNotFoundError(f"监控程序不存在：{MONITOR_SCRIPT}")

        creationflags = 0
        if self.platform_name == "win32":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )
        self.popen(
            [
                sys.executable,
                str(MONITOR_SCRIPT),
                "--config",
                str(self.config_path),
            ],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )

        deadline = self.monotonic() + max(0.1, float(startup_timeout))
        while self.monotonic() < deadline:
            status = self.status()
            if status.get("running"):
                return status
            self.sleeper(0.1)
        raise RuntimeError("监控进程启动超时，请查看 data/logs/json_monitor.log")

    def stop(self) -> dict:
        return self._request("/shutdown", method="POST", timeout=2.0)

    def run_now(self) -> dict:
        return self._request("/run-now", method="POST", timeout=2.0)


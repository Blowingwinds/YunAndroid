"""Feishu custom-bot webhook client for JSON monitoring alerts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class FeishuNotifier:
    def __init__(
        self,
        webhook_url: str = "",
        *,
        secret: str = "",
        timeout: float = 10.0,
        opener: Callable = urlopen,
        clock: Callable[[], float] = time.time,
    ):
        normalized_webhook = str(webhook_url).strip()
        parsed = urlparse(normalized_webhook)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("飞书机器人 webhook_url 必须是有效的 HTTP(S) 地址")
        self.webhook_url = normalized_webhook
        self.secret = str(secret).strip()
        self.timeout = float(timeout)
        self.opener = opener
        self.clock = clock

    @staticmethod
    def _signature(timestamp: str, secret: str) -> str:
        signing_key = f"{timestamp}\n{secret}".encode("utf-8")
        digest = hmac.new(signing_key, digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def _sign_payload(self, payload: dict[str, object]) -> None:
        if self.secret:
            timestamp = str(int(self.clock()))
            payload.update(
                timestamp=timestamp,
                sign=self._signature(timestamp, self.secret),
            )

    def _post(self, payload: dict[str, object]) -> dict:
        self._sign_payload(payload)
        request = Request(
            self.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with self.opener(request, timeout=self.timeout) as response:
            raw = response.read()
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("飞书机器人返回了无效响应") from exc

        code = result.get("code", result.get("StatusCode"))
        if code != 0:
            message = result.get("msg", result.get("StatusMessage", "未知错误"))
            raise RuntimeError(f"飞书机器人发送失败：{message}")
        return result

    def send_text(self, text: str) -> dict:
        payload: dict[str, object] = {
            "msg_type": "text",
            "content": {"text": str(text)},
        }
        return self._post(payload)


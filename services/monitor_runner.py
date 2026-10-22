"""One-round orchestration and persistent alert state for JSON monitoring."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from services.device_operations import ADB_PATH, DeviceOperations, validate_package_name
from services.device_repository import Device, load_devices
from services.feishu_notifier import FeishuNotifier
from services.monitoring import (
    BEIJING_TZ,
    AlertResult,
    MonitorConfig,
    Observation,
    build_alert_message,
    evaluate_observation,
)
from services.openSSH import TunnelManager


MONITORED_TAG = "日常监控"
ALERT_DISABLED_NOTE_KEYWORD = "关闭"


def device_monitor_key(device: Device) -> str:
    """Return a stable connection-and-package key independent of display IDs."""
    return "|".join(
        (
            device.public_ip.strip(),
            device.remote_ip.strip(),
            str(device.remote_port),
            device.package_name.strip(),
        )
    )


class MonitorStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_state (
                    monitor_date TEXT NOT NULL,
                    device_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    notified INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY (monitor_date, device_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def record_evaluations(
        self,
        results: Iterable[AlertResult],
        *,
        monitor_date: str,
        observed_at: str,
    ) -> list[AlertResult]:
        pending: list[AlertResult] = []
        with self._connect() as connection:
            for result in results:
                row = connection.execute(
                    """
                    SELECT status, active, notified, first_seen_at
                    FROM alert_state
                    WHERE monitor_date = ? AND device_key = ?
                    """,
                    (monitor_date, result.device_key),
                ).fetchone()
                same_incident = bool(
                    row
                    and row["active"]
                    and row["status"] == result.status
                    and result.is_alert
                )
                notified = int(row["notified"]) if same_incident else 0
                first_seen_at = str(row["first_seen_at"]) if same_incident else observed_at
                active = int(result.is_alert)
                connection.execute(
                    """
                    INSERT INTO alert_state (
                        monitor_date, device_key, status, active, notified,
                        first_seen_at, last_seen_at, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(monitor_date, device_key) DO UPDATE SET
                        status = excluded.status,
                        active = excluded.active,
                        notified = excluded.notified,
                        first_seen_at = excluded.first_seen_at,
                        last_seen_at = excluded.last_seen_at,
                        reason = excluded.reason
                    """,
                    (
                        monitor_date,
                        result.device_key,
                        result.status,
                        active,
                        notified,
                        first_seen_at,
                        observed_at,
                        result.reason,
                    ),
                )
                if result.is_alert and not notified:
                    pending.append(result)
        return pending

    def mark_notified(self, monitor_date: str, results: Iterable[AlertResult]) -> None:
        with self._connect() as connection:
            connection.executemany(
                """
                UPDATE alert_state
                SET notified = 1
                WHERE monitor_date = ? AND device_key = ? AND status = ? AND active = 1
                """,
                [
                    (monitor_date, result.device_key, result.status)
                    for result in results
                ],
            )

    def set_runtime(self, **values: object) -> None:
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO runtime_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                [
                    (key, json.dumps(value, ensure_ascii=False))
                    for key, value in values.items()
                ],
            )

    def get_runtime(self) -> dict[str, object]:
        with self._connect() as connection:
            rows = connection.execute("SELECT key, value FROM runtime_state").fetchall()
        result = {}
        for row in rows:
            try:
                result[str(row["key"])] = json.loads(row["value"])
            except json.JSONDecodeError:
                result[str(row["key"])] = row["value"]
        return result


class MonitorRunner:
    def __init__(
        self,
        config: MonitorConfig,
        *,
        load_devices_fn: Callable[[Path], list[Device]] = load_devices,
        tunnel_manager_factory: Callable[[], TunnelManager] | None = None,
        operations: DeviceOperations | None = None,
        notifier: FeishuNotifier | None = None,
        state_store: MonitorStateStore | None = None,
        logger: Callable[[str], None] = print,
    ):
        self.config = config
        self.load_devices_fn = load_devices_fn
        self.tunnel_manager_factory = tunnel_manager_factory or (
            lambda: TunnelManager(adb_executable=str(ADB_PATH))
        )
        self.operations = operations or DeviceOperations()
        self.notifier = notifier or FeishuNotifier(
            config.feishu_webhook_url,
            secret=config.feishu_secret,
        )
        self.state_store = state_store or MonitorStateStore(config.state_db)
        self.logger = logger

    @staticmethod
    def _moment(now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(BEIJING_TZ)
        if now.tzinfo is None:
            return now.replace(tzinfo=BEIJING_TZ)
        return now.astimezone(BEIJING_TZ)

    @staticmethod
    def _observation(
        device: Device,
        *,
        status: str,
        detail: str = "",
        metadata: dict | None = None,
    ) -> Observation:
        values = metadata or {}
        return Observation(
            device_key=device_monitor_key(device),
            device_id=device.id,
            app_name=device.app_name,
            group=device.group,
            remote_address=device.remote_address,
            status=status,
            mtime_epoch=values.get("mtime_epoch"),
            size=values.get("size"),
            device_epoch=values.get("device_epoch"),
            detail=detail,
        )

    def _probe_device(self, device: Device, date_text: str) -> Observation:
        try:
            metadata = self.operations.inspect_daily_json(
                device,
                device.package_name,
                date_text,
            )
        except Exception as exc:
            return self._observation(device, status="unreachable", detail=str(exc))
        status = "ok" if metadata.get("exists") else "missing"
        return self._observation(device, status=status, metadata=metadata)

    def _collect_observations(
        self,
        devices: list[Device],
        date_text: str,
    ) -> list[Observation]:
        observations: list[Observation] = []
        keys = [device_monitor_key(device) for device in devices]
        duplicate_keys = {key for key, count in Counter(keys).items() if count > 1}
        valid_devices: list[Device] = []
        for device in devices:
            key = device_monitor_key(device)
            if not device.package_name.strip():
                observations.append(
                    self._observation(device, status="config_error", detail="APK 包名为空")
                )
                continue
            try:
                validate_package_name(device.package_name)
            except ValueError as exc:
                observations.append(
                    self._observation(device, status="config_error", detail=str(exc))
                )
                continue
            if key in duplicate_keys:
                observations.append(
                    self._observation(device, status="config_error", detail="监控设备键重复")
                )
                continue
            valid_devices.append(device)

        if not valid_devices:
            return observations

        manager = self.tunnel_manager_factory()
        mappings = [
            {
                "device_id": device_monitor_key(device),
                "public_ip": device.public_ip,
                "local_port": device.local_port,
                "remote_ip": device.remote_ip,
                "remote_port": device.remote_port,
            }
            for device in valid_devices
        ]
        try:
            records = manager.connect_mappings(mappings)
            records_by_key = {str(record.get("device_id", "")): record for record in records}
            probe_targets: list[Device] = []
            for device in valid_devices:
                record = records_by_key.get(device_monitor_key(device), {})
                if record.get("adb_status") not in {"成功", "已连接"}:
                    observations.append(
                        self._observation(
                            device,
                            status="unreachable",
                            detail=str(record.get("reason") or "ADB 未连接"),
                        )
                    )
                    continue
                probe_targets.append(
                    replace(device, local_port=int(record.get("local_port", device.local_port)))
                )

            with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
                futures = {
                    executor.submit(self._probe_device, device, date_text): device
                    for device in probe_targets
                }
                for future in as_completed(futures):
                    observations.append(future.result())
        finally:
            manager.disconnect()
        return observations

    def run_once(self, *, now: datetime | None = None) -> dict[str, object]:
        moment = self._moment(now)
        date_text = moment.date().isoformat()
        devices = [
            device
            for device in self.load_devices_fn(self.config.queue_file)
            if (
                device.enabled
                and device.tag.strip() == MONITORED_TAG
                and ALERT_DISABLED_NOTE_KEYWORD not in device.note
            )
        ]
        observations = self._collect_observations(devices, date_text)
        evaluations = [
            evaluate_observation(observation, moment, self.config)
            for observation in observations
        ]
        self.state_store.record_evaluations(
            evaluations,
            monitor_date=date_text,
            observed_at=moment.isoformat(),
        )
        active = [result for result in evaluations if result.is_alert]
        notification_error = ""
        if active:
            message = build_alert_message(
                now=moment,
                total=len(devices),
                security_keyword=self.config.feishu_security_keyword,
                new_alerts=active,
            )
            try:
                self.notifier.send_text(message)
            except Exception as exc:
                notification_error = str(exc)
                self.logger(f"飞书告警发送失败：{exc}")
            else:
                self.state_store.mark_notified(date_text, active)

        counts = Counter(result.status for result in evaluations)
        summary: dict[str, object] = {
            "checked_at": moment.isoformat(),
            "date": date_text,
            "total": len(devices),
            "normal": counts.get("normal", 0),
            "active_alerts": len(active),
            "new_alerts": len(active),
            "notification_error": notification_error,
        }
        summary.update({status: count for status, count in counts.items()})
        self.state_store.set_runtime(last_summary=summary, last_finished_at=moment.isoformat())
        return summary


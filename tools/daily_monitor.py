"""按标签批量拉取云手机上的日期 JSON 数据。

流程：

    1. services/device_repository.load_devices  读取 config/cloud_phone_queue.xlsx
    2. 按 tag 筛选设备（--tag，默认「重点私服」）
    3. services/openSSH.TunnelManager           建立 SSH 隧道并完成 adb connect
    4. services/device_operations.DeviceOperations 拉取 /data/data/{包名}/files/data/{日期}.json
    5. 落盘到 SF_json/{标签}_{日期}_json/，文件名为 应用名_区组名_包名.json
    6. 收尾：释放本次建立的隧道

网络前提：通过 YUNANDROID_SSH_USER_HOST 配置 SSH 跳板机，
本流程**直连**即可完成，不涉及任何 VPN 拨号。

命令行::

    python tools/daily_monitor.py run --date 2026-10-16        # 完整流程（run 可省略）
    python tools/daily_monitor.py --date yesterday             # 同上
    python tools/daily_monitor.py --date today --dry-run       # 只打印计划，不连接不拉取
    python tools/daily_monitor.py --date today --tag 日常监控   # 换标签
    python tools/daily_monitor.py --date today --limit 3       # 先试 3 台
    python tools/daily_monitor.py --date today --verbose       # 日志文件记录 DEBUG 中间态
    python tools/daily_monitor.py cleanup                      # 清理强杀残留的隧道/adb
    python tools/daily_monitor.py schedule                     # 常驻定时：每天 10:00 拉「重点私服」
    python tools/daily_monitor.py schedule --at 10:30          # 改触发时刻
    python tools/daily_monitor.py schedule --date yesterday    # 每次跑批取前一天数据
    python tools/daily_monitor.py schedule --run-on-start      # 启动即跑一次，之后按点等待
    python tools/daily_monitor.py schedule --once              # 只跑到下一个触发点执行一次就退出（便于验证）

日志默认只记 INFO（每台一行进度 + 阶段标题），排查疑难时加 --verbose 记录 DEBUG。

定时任务（schedule 子命令）
--------------------------

常驻进程，每天固定时刻跑一次完整流程，并在本地记录结果。
触发时刻、标签、数据日期等默认值写在 config/ui_settings.ini 的 [schedule] 段：

    [schedule]
    enabled=1          ; 0 时 schedule 子命令直接退出
    time=10:00         ; 每日触发时刻
    tag=重点私服        ; 拉取标签
    date=yesterday      ; 数据日期，默认 yesterday（拉前一天）
    run_on_start=0     ; 1=启动即跑一次
    catchup_minutes=120 ; 休眠/卡顿错过触发点后，仍允许补跑的时间窗口

命令行参数优先级高于 ini。调度器只用标准库实现（datetime + time.sleep），
不依赖三方包；错过触发点在 catchup_minutes 内会立即补跑一次，超窗则自动顺延到下一次，
不会在恢复后连续追跑。

也可以作为库调用::

    DailyMonitorRunner().run("2026-10-16")
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date as date_type
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Sequence

# 支持 `python tools/daily_monitor.py` 直接运行：先把项目根加入模块搜索路径。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.device_operations import ADB_PATH, DeviceOperations, validate_package_name, validate_remote_file_path
from services.device_repository import QUEUE_FILE, Device, load_devices
from services.openSSH import TunnelManager, configure_console_encoding
from services.screenshotUtil import safe_filename

# ---------- 日常监控常量 ----------

DEFAULT_TAG = "日常监控"
LOG_ROOT = PROJECT_ROOT / "data" / "logs"
OUTPUT_ROOT = PROJECT_ROOT / "SF_json"
OUTPUT_DIR_TEMPLATE = "{tag}_{date}_json"
REMOTE_DIR_TEMPLATE = "/data/data/{package_name}/files/data"
REMOTE_FILE_TEMPLATE = "{directory}/{date}.{extension}"
DEFAULT_EXTENSION = "json"
DEFAULT_TUNNEL_WAIT = 10.0
SETTINGS_FILE = PROJECT_ROOT / "config" / "ui_settings.ini"

ADB_STATUS_SUCCESS = "成功"

# ---------- 定时任务常量 ----------

SCHEDULE_DEFAULT_TIME = "10:00"      # 默认每日触发时刻
SCHEDULE_DEFAULT_TAG = DEFAULT_TAG   # 定时任务默认拉取的标签
SCHEDULE_DEFAULT_DATE = "yesterday"  # 定时任务默认取数日期（10 点跑前一天日报）
SCHEDULE_SECTION = "schedule"        # config/ui_settings.ini 的 [schedule] 段
SCHEDULE_POLL_SECONDS = 20.0         # 等待期间的唤醒间隔（兼顾休眠唤醒与 CPU 占用）
SCHEDULE_CATCHUP_MINUTES = 120       # 错过触发点后仍允许补跑的时间窗口
SCHEDULE_LOG_NAME = "daily_monitor_schedule"

# ---------- 日常监控：工具函数 ----------


def normalize_date(value: str | None = None) -> str:
    """把 today / yesterday / 2026-10-16 / 20261016 统一成 YYYY-MM-DD。"""
    today = date_type.today()
    text = (value or "today").strip().lower()
    if text in ("today", "今天"):
        target = today
    elif text in ("yesterday", "昨天"):
        target = today - timedelta(days=1)
    else:
        candidate = text.replace("/", "-").replace(".", "-")
        parsed = None
        for pattern in ("%Y-%m-%d", "%Y%m%d"):
            try:
                parsed = datetime.strptime(candidate, pattern).date()
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"日期格式不正确（支持 today / yesterday / YYYY-MM-DD）：{value}")
        target = parsed
    return target.isoformat()


def build_remote_file_path(
    package_name: str,
    date_text: str,
    directory_template: str = REMOTE_DIR_TEMPLATE,
    extension: str = DEFAULT_EXTENSION,
) -> str:
    """拼 `{目录}/{日期}.{扩展名}` 远端路径，目录模板支持 {package_name} 占位。

    默认情况下就是 /data/data/{包名}/files/data/{日期}.json；
    部分游戏把日报放在 files/logs/{日期}.log，用 --remote-dir/--remote-ext 参数切换即可。
    """
    try:
        safe_package_name = validate_package_name(package_name)
    except ValueError as exc:
        raise ValueError(f"设备包名不合法：{exc}") from exc
    suffix = str(extension).strip().lstrip(".") or DEFAULT_EXTENSION
    try:
        directory = str(directory_template).format(package_name=safe_package_name).rstrip("/")
    except (KeyError, IndexError) as exc:
        raise ValueError(f"远端目录模板不合法：{exc}") from exc
    return validate_remote_file_path(
        REMOTE_FILE_TEMPLATE.format(directory=directory, date=date_text, extension=suffix)
    )


def build_output_directory(tag: str, date_text: str, output_root: str | Path = OUTPUT_ROOT) -> Path:
    """本地落盘目录：SF_json/{标签}_{日期}_json，如 SF_json/重点私服_2026-10-16_json。"""
    safe_tag = safe_filename((tag or "").strip()) or "default"
    return Path(output_root) / OUTPUT_DIR_TEMPLATE.format(tag=safe_tag, date=date_text)


def build_local_filename(device: Device, extension: str = DEFAULT_EXTENSION) -> str:
    """本地文件名：应用名_区组名_包名.{扩展名}，字段缺失时用占位符补齐。"""
    parts = []
    for value, fallback in (
        (device.app_name, "unknown_app"),
        (device.group, "unknown_group"),
        (device.package_name, "unknown_package"),
    ):
        parts.append(safe_filename((value or "").strip()) or fallback)
    suffix = str(extension).strip().lstrip(".") or DEFAULT_EXTENSION
    return "_".join(parts) + "." + suffix


def select_devices(
    devices: Sequence[Device],
    tag: str = DEFAULT_TAG,
    *,
    include_disabled: bool = False,
) -> list[Device]:
    """按标签筛选设备，默认跳过停用记录。"""
    target = tag.strip()
    selected = []
    for device in devices:
        if (device.tag or "").strip() != target:
            continue
        if not device.enabled and not include_disabled:
            continue
        selected.append(device)
    return selected


def build_mappings(devices: Sequence[Device]) -> list[dict]:
    """把设备记录转成 TunnelManager 需要的端口转发映射。"""
    return [
        {
            "device_id": device.id,
            "public_ip": device.public_ip,
            "local_port": device.local_port,
            "remote_ip": device.remote_ip,
            "remote_port": device.remote_port,
        }
        for device in devices
    ]


def short_path(path: str | Path | None) -> str:
    """把绝对路径缩成「相对项目根」的形式，日志里少占一半宽度。"""
    if not path:
        return "-"
    text = str(path)
    try:
        return str(Path(text).resolve().relative_to(PROJECT_ROOT))
    except (ValueError, OSError):
        return text


def format_size(size: int | bytes | None) -> str:
    """字节数转人类可读，0/None 统一显示为 0 B。"""
    value = int(size or 0)
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / 1024 / 1024:.2f} MB"


# adb 的报错又长又重复，压成一行短句；完整原文仍留在 DEBUG 里。
# (报错特征, 短句, 排查建议)
_ERROR_PATTERNS = (
    (
        "No such file or directory",
        "远端文件不存在",
        "该设备当天未生成数据文件（应用未启动 / 未跨天结算），可换 --date 重试或跳过",
    ),
    (
        "Permission denied",
        "权限不足（设备未 root）",
        "需 root 才能读取 /data/data，adb root 后重试，或改用 --remote-dir 指向可读目录",
    ),
    (
        "device unauthorized",
        "adb 未授权",
        "在设备上确认「允许 USB 调试」弹窗，或执行 adb kill-server 后重连",
    ),
    (
        "device offline",
        "adb 掉线",
        "隧道仍在但 adb 会话已断，重跑本脚本的 cleanup 子命令后再试",
    ),
    (
        "error: closed",
        "adb 连接中断",
        "设备侧主动断开，多为隧道瞬断；重跑即可，频繁出现需检查跳板机稳定性",
    ),
    (
        "error: cannot connect",
        "adb 无法连接",
        "本地端口未监听或被占用，先 cleanup 再重跑",
    ),
    (
        "remote object",
        "远端对象不可访问",
        "adb 无法 stat 该路径，确认路径与包名匹配",
    ),
    (
        "protocol fault",
        "adb 协议错误",
        "传输层异常，通常是隧道被干扰，重跑",
    ),
    (
        "connection reset",
        "连接被重置",
        "SSH 隧道或设备侧断流，重跑",
    ),
    (
        "is empty",
        "目标文件为空",
        "远端文件存在但内容为 0 字节，设备端写入尚未完成",
    ),
)


def diagnose_error(message: str) -> tuple[str, str]:
    """把报错压成 (短句, 排查建议)。认不出的模式返回 ('', '')。"""
    text = " ".join(str(message).split())
    if not text:
        return "未知错误", "报错为空，请用 --verbose 复现"
    for needle, short, hint in _ERROR_PATTERNS:
        if needle in text:
            return short, hint
    return "", ""


def condense_error(message: str, limit: int = 60) -> str:
    """把 adb/adb server 的长报错压成一行短句。

    「adb: error: failed to stat remote object 'xxx': No such file or directory」
    → 「远端文件不存在」；认不出的模式优先保留 "xxx: 原因" 里的原因部分，再按 limit 截断。
    """
    text = " ".join(str(message).split())
    if not text:
        return "未知错误"
    short, _hint = diagnose_error(text)
    if short:
        return short
    if len(text) <= limit:
        return text
    # 优先保留 "xxx: 原因" 里的原因部分（冒号右边的通常是真正的错误码）。
    if ": " in text:
        tail = text.rsplit(": ", 1)[1].strip()
        if tail and len(tail) < len(text):
            text = tail
    return text if len(text) <= limit else text[:limit] + "…"


def format_failure_detail(result: "DeviceResult") -> str:
    """把失败诊断渲染成多行明细块，供日志 WARNING 直接打印。

    正常情况不会被调用，所以失败时给足信息、成功时不增加噪音。
    """
    lines = [
        f"    ── 失败明细 · {result.device_id} · {result.app_name or '-'} ──",
        f"    阶段：{result.stage or '未知'}",
        f"    队列：{result.device_id} | 包名={result.package_name or '-'} | 远端={result.remote_address}",
    ]
    if result.local_port is not None:
        lines.append(f"    隧道：{result.tunnel_status or '-'} local={result.local_port}")
    if result.remote_path:
        lines.append(f"    远端路径：{result.remote_path}")
    if result.target_name:
        lines.append(f"    目标文件：{result.target_name}")
    if result.error:
        lines.append(f"    原始报错：{result.error}")
    if result.hint:
        lines.append(f"    排查建议：{result.hint}")
    if result.traceback:
        lines.append(f"    异常堆栈：\n{result.traceback}")
    return "\n".join(lines)


def setup_logger(date_text: str, *, console: bool = True, verbose: bool = False) -> logging.Logger:
    """日志写入 data/logs/daily_monitor_{日期}.log，并同步输出到标准输出。

    verbose=True 时把 DEBUG 中间态（远端路径、端口重分配等）也写进文件，
    排查疑难问题时用；默认只记 INFO，日志体积约为原来的 1/5。
    """
    logger = logging.getLogger(f"daily_monitor.{date_text}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = LOG_ROOT / f"daily_monitor_{date_text.replace('-', '')}.log"
    if not any(
        isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename) == log_path
        for handler in logger.handlers
    ):
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
        )
        logger.addHandler(file_handler)
    if console and not any(
        getattr(handler, "_daily_monitor_console", False) for handler in logger.handlers
    ):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler._daily_monitor_console = True
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console_handler)
    return logger


@dataclass
class DeviceResult:
    device_id: str
    app_name: str
    package_name: str
    remote_address: str
    status: str
    detail: str = ""
    tunnel_status: str = ""
    local_port: int | None = None
    local_path: str = ""
    size: int = 0
    elapsed: float = 0.0
    group: str = ""
    # ---- 失败诊断（仅失败时填充，供日志和本地汇总使用）----
    stage: str = ""          # 失败发生在哪一步：隧道 / adb校验 / 路径 / 拉取 / 落盘
    remote_path: str = ""    # 实际尝试拉取的远端路径
    target_name: str = ""    # 期望的本地文件名
    error: str = ""          # 未经压缩的原始报错全文
    hint: str = ""           # 基于错误模式给出的排查建议
    traceback: str = ""      # Python 堆栈（仅非 adb 的意外异常才有）


class DailyMonitorRunner:
    """串起「读队列 → 筛标签 → 建隧道 → 拉文件 → 收尾」的一体化流程。"""

    def __init__(
        self,
        *,
        queue_file: str | Path = QUEUE_FILE,
        output_root: str | Path = OUTPUT_ROOT,
        tag: str = DEFAULT_TAG,
        include_disabled: bool = False,
        operations: DeviceOperations | None = None,
        tunnel_manager: TunnelManager | None = None,
        adb_path: str | Path = ADB_PATH,
        tunnel_wait: float = DEFAULT_TUNNEL_WAIT,
        keep_tunnels: bool = False,
        limit: int | None = None,
        remote_dir: str = REMOTE_DIR_TEMPLATE,
        remote_ext: str = DEFAULT_EXTENSION,
        max_workers: int = 1,
        logger: logging.Logger | None = None,
    ) -> None:
        self.queue_file = Path(queue_file)
        self.output_root = Path(output_root)
        self.tag = tag
        self.include_disabled = include_disabled
        self.operations = operations or DeviceOperations()
        self.tunnel_manager = tunnel_manager or TunnelManager(
            adb_executable=str(adb_path), tunnel_wait=tunnel_wait
        )
        self.keep_tunnels = keep_tunnels
        self.limit = limit
        self.remote_dir = remote_dir
        self.remote_ext = remote_ext
        self.max_workers = int(max_workers)
        if not 1 <= self.max_workers <= 16:
            raise ValueError("max_workers 必须在 1 到 16 之间")
        self._logger = logger
        self._active_records: list[dict] = []

    # ---------- 日志 ----------

    @property
    def logger(self) -> logging.Logger:
        if self._logger is None:
            self._logger = setup_logger(datetime.now().strftime("%Y-%m-%d"))
        return self._logger

    # ---------- 主流程 ----------

    def run(self, date_text: str | None = None, *, dry_run: bool = False) -> dict:
        """执行一次完整拉取（队列 → 筛选 → 隧道 → 拉取 → 收尾），返回汇总字典。"""
        started_at = datetime.now()
        try:
            target_date = normalize_date(date_text)
        except ValueError as exc:
            self.logger.error("参数错误：%s", exc)
            raise
        if self._logger is None:
            self._logger = setup_logger(target_date)
        log = self.logger

        log.info("=" * 60)
        log.info(
            "拉取开始 | 日期=%s | 标签=%s | 队列=%s",
            target_date,
            self.tag,
            short_path(self.queue_file),
        )

        devices = load_devices(self.queue_file)
        selected = select_devices(devices, self.tag, include_disabled=self.include_disabled)
        log.info(
            "队列 %d 条 → 标签「%s」命中 %d 台%s",
            len(devices),
            self.tag,
            len(selected),
            f"（仅前 {self.limit} 台）" if self.limit and len(selected) > self.limit else "",
        )
        if not selected:
            log.warning("没有匹配的设备，任务结束")
            return self._build_summary(target_date, started_at, [], skipped=len(devices))

        unusable = [device for device in selected if not (device.package_name or "").strip()]
        ready = [device for device in selected if device not in unusable]
        if unusable:
            log.warning(
                "缺包名跳过 %d 台：%s",
                len(unusable),
                ", ".join(device.id for device in unusable[:10])
                + ("…" if len(unusable) > 10 else ""),
            )

        if self.limit and self.limit > 0 and len(ready) > self.limit:
            log.info("--limit 生效，仅处理前 %d 台", self.limit)
            ready = ready[: self.limit]

        results: list[DeviceResult] = [
            DeviceResult(
                device_id=device.id,
                app_name=device.app_name,
                package_name=device.package_name,
                remote_address=device.remote_address,
                status="已跳过",
                detail="队列记录缺少包名",
                group=device.group,
            )
            for device in unusable
        ]

        destination = build_output_directory(self.tag, target_date, self.output_root)

        if dry_run:
            log.info("试运行：以下为计划，不建立隧道、不拉取文件")
            for device in ready:
                remote_path = build_remote_file_path(
                    device.package_name, target_date, self.remote_dir, self.remote_ext
                )
                log.info(
                    "    %s %s → %s",
                    device.id,
                    remote_path,
                    short_path(destination / build_local_filename(device, self.remote_ext)),
                )
            summary = self._build_summary(
                target_date,
                started_at,
                results
                + [
                    DeviceResult(
                        d.id,
                        d.app_name,
                        d.package_name,
                        d.remote_address,
                        "待执行",
                        detail="试运行",
                        group=d.group,
                    )
                    for d in ready
                ],
            )
            return summary

        try:
            mappings = build_mappings(ready)
            reserved_ports = {
                device.local_port for device in devices if device.id not in {item.id for item in ready}
            }
            log.info(
                "建立 SSH 隧道 + adb：%d 条映射（保留端口 %d）",
                len(mappings),
                len(reserved_ports),
            )
            records = self.tunnel_manager.connect_mappings(mappings, reserved_ports=reserved_ports)
            self._active_records = list(records)
            by_device = {record.get("device_id"): record for record in records}

            bad_tunnels = [
                record for record in records if record.get("adb_status") != ADB_STATUS_SUCCESS
            ]
            ready_tunnels = len(records) - len(bad_tunnels)
            for record in bad_tunnels:
                raw_reason = str(record.get("reason") or record.get("adb_status"))
                short, hint = diagnose_error(raw_reason)
                log.error(
                    "    ── 隧道未就绪 · %s ──\n"
                    "    阶段：隧道建立\n"
                    "    local=%s remote=%s:%s\n"
                    "    原因：%s\n"
                    "    原始：%s\n"
                    "    建议：%s",
                    record.get("device_id"),
                    record.get("local_port"),
                    record.get("remote_ip"),
                    record.get("remote_port"),
                    short or condense_error(raw_reason),
                    raw_reason,
                    hint or "检查跳板机连通性与本地端口占用",
                )
            log.info("隧道就绪 %d/%d", ready_tunnels, len(records))

            destination.mkdir(parents=True, exist_ok=True)
            worker_count = max(1, min(self.max_workers, len(ready)))
            log.info(
                "受控并发拉取（并发数 %d）→ %s",
                worker_count,
                short_path(destination),
            )

            def process_device(device: Device) -> tuple[Device, DeviceResult]:
                return device, self._process_device(
                    device, target_date, destination, by_device.get(device.id)
                )

            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="daily-monitor-pull",
            ) as executor:
                processed = executor.map(process_device, ready)
                for index, (device, result) in enumerate(processed, start=1):
                    results.append(result)
                    # 每台一行进度，替代原先 7 行中间态；失败/跳过用 WARNING 便于过滤
                    line = "    [%d/%d] %s %s %s"
                    args = (index, len(ready), device.id, result.status, result.detail or "-")
                    if result.status == "成功":
                        log.info(line, *args)
                    else:
                        log.warning(line, *args)
                        if result.status == "失败":
                            # 成功路径不增加任何噪音；失败才给足诊断明细
                            log.warning("\n%s", format_failure_detail(result))
        finally:
            log.info("收尾：释放本次建立的隧道")
            self.close_tunnels()

        summary = self._build_summary(target_date, started_at, results)
        return summary

    def _process_device(
        self,
        device: Device,
        date_text: str,
        destination: Path,
        record: dict | None,
    ) -> DeviceResult:
        log = self.logger
        started = time.perf_counter()
        result = DeviceResult(
            device_id=device.id,
            app_name=device.app_name,
            package_name=device.package_name,
            remote_address=device.remote_address,
            status="失败",
            group=device.group,
        )
        try:
            if record is None:
                result.status = "失败"
                result.stage = "隧道"
                result.detail = "隧道记录缺失"
                result.error = "connect_mappings 未返回该设备的隧道记录"
                result.hint = "通常是端口分配冲突，先跑 cleanup 子命令再重试"
                return self._finish(result, started)

            tunnel_status = str(record.get("adb_status", ""))
            result.tunnel_status = tunnel_status
            result.local_port = int(record.get("local_port", device.local_port))
            if tunnel_status != ADB_STATUS_SUCCESS:
                raw_reason = str(record.get("reason", tunnel_status))
                short, hint = diagnose_error(raw_reason)
                result.stage = "隧道"
                result.detail = "隧道/ADB 未就绪：" + (short or condense_error(raw_reason))
                result.error = raw_reason
                result.hint = hint or "检查 SSH 跳板机连通性与本地端口占用"
                return self._finish(result, started)

            # 端口可能因为占用被重新分配，用实际端口重建设备序列。
            effective = replace(device, local_port=result.local_port)
            if result.local_port != device.local_port:
                log.debug(
                    "    %s 本地端口 %s 被占用，改用 %s",
                    device.id,
                    device.local_port,
                    result.local_port,
                )

            try:
                self.operations.check_adb_ready(effective)
            except Exception as exc:
                short, hint = diagnose_error(str(exc))
                result.stage = "ADB 校验"
                result.detail = short or condense_error(str(exc))
                result.error = str(exc)
                result.hint = hint or "adb devices 状态异常，跑 cleanup 清理后重试"
                return self._finish(result, started)

            remote_path = build_remote_file_path(
                effective.package_name, date_text, self.remote_dir, self.remote_ext
            )
            target_name = build_local_filename(effective, self.remote_ext)
            target_path = destination / target_name
            result.local_path = str(target_path)
            result.remote_path = remote_path
            result.target_name = target_name

            staging_prefix = f".pull-{safe_filename(effective.id)}-"
            with TemporaryDirectory(prefix=staging_prefix, dir=destination) as staging:
                local_path = self.operations.pull_remote_file(
                    effective, remote_path, staging
                )
                Path(local_path).replace(target_path)
            result.local_path = str(target_path)
            result.size = target_path.stat().st_size
            result.status = "成功"
            result.detail = format_size(result.size)
        except ValueError as exc:
            # 路径/包名不合法这类本地错误，远端路径根本没用上
            result.stage = "路径构造"
            result.detail = condense_error(str(exc))
            result.error = str(exc)
            result.hint = "检查 --remote-dir 模板与队列里的包名是否正确"
        except Exception as exc:  # noqa: BLE001 - 单台失败不影响其余设备
            result.stage = "拉取"
            short, hint = diagnose_error(str(exc))
            result.detail = short or condense_error(str(exc))
            result.error = str(exc)
            result.hint = hint or "用 --verbose 复现可拿到完整堆栈"
            if not short:  # 非已知 adb 错误才算意外异常，堆栈才有排查价值
                result.traceback = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ).rstrip()
        return self._finish(result, started)

    @staticmethod
    def _finish(result: DeviceResult, started: float) -> DeviceResult:
        result.elapsed = round(time.perf_counter() - started, 2)
        return result

    def close_tunnels(self) -> None:
        """断开本次流程建立的隧道与 adb 连接。"""
        if self.keep_tunnels or not self._active_records:
            if self._active_records:
                self.logger.info("保留隧道（--keep-tunnels），共 %d 条", len(self._active_records))
            return
        try:
            removed = self.tunnel_manager.disconnect_mappings(self._active_records)
            self.logger.info("已断开隧道 %d 条", len(removed))
        except Exception as exc:  # noqa: BLE001 - 收尾失败不应中断统计
            self.logger.error("断开隧道时出错：%s", exc)
        finally:
            self._active_records = []

    def _build_summary(
        self, date_text: str, started_at: datetime, results: list[DeviceResult], *, skipped: int = 0
    ) -> dict:
        finished_at = datetime.now()
        summary = {
            "date": date_text,
            "tag": self.tag,
            "queue_file": str(self.queue_file),
            "output_root": str(self.output_root),
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "elapsed_seconds": round((finished_at - started_at).total_seconds(), 2),
            "total": len(results),
            "success": sum(1 for item in results if item.status == "成功"),
            "failed": sum(1 for item in results if item.status == "失败"),
            "skipped": skipped + sum(1 for item in results if item.status in ("已跳过",)),
            "results": [item.__dict__ for item in results],
        }
        for item in results:
            self.logger.debug(
                "结果 %s | %s | %s | %s | %.2fs",
                item.device_id,
                item.app_name or "-",
                item.status,
                item.detail or "-",
                item.elapsed,
            )
        # 异常设备已在进度行用 WARNING 逐条打过，这里不再重复列举，只给一个提示。
        abnormal = sum(1 for item in results if item.status in ("失败", "已跳过"))
        if abnormal:
            self.logger.info("（上方标为 失败/已跳过 的 %d 台为异常设备）", abnormal)
        self.logger.info(
            "完成：共 %d 台，成功 %d，失败 %d，跳过 %d，耗时 %.2f 秒",
            summary["total"],
            summary["success"],
            summary["failed"],
            summary["skipped"],
            summary["elapsed_seconds"],
        )
        return summary


# ---------- 定时任务：配置 ----------


def _str_to_bool(text: str, *, default: bool = False) -> bool:
    """ini 里的开关值转布尔，非法值回落到 default。"""
    raw = str(text).strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    return default


def load_schedule_settings(settings_file: str | Path = SETTINGS_FILE) -> dict:
    """读取 ini 的 [schedule] 段：触发时刻、标签、取数日期、补跑窗口等默认值。

    文件缺失/写坏/缺少该段一律返回内置默认值，绝不让配置问题卡住调度器启动。
    """
    defaults = {
        "enabled": True,
        "time": SCHEDULE_DEFAULT_TIME,
        "tag": SCHEDULE_DEFAULT_TAG,
        "date": SCHEDULE_DEFAULT_DATE,
        "run_on_start": False,
        "catchup_minutes": SCHEDULE_CATCHUP_MINUTES,
    }
    # inline_comment_prefixes 必须显式打开：默认的 configparser 会把
    # `time=10:00  ; 每日触发时刻` 整段（含注释）当成值读进来。
    parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    try:
        if not Path(settings_file).exists():
            return defaults
        parser.read(settings_file, encoding="utf-8")
    except (OSError, configparser.Error):
        return defaults
    if not parser.has_section(SCHEDULE_SECTION):
        return defaults
    section = parser[SCHEDULE_SECTION]
    defaults["enabled"] = _str_to_bool(section.get("enabled", fallback="1"), default=True)
    defaults["time"] = section.get("time", fallback=SCHEDULE_DEFAULT_TIME).strip() or SCHEDULE_DEFAULT_TIME
    defaults["tag"] = section.get("tag", fallback=SCHEDULE_DEFAULT_TAG).strip() or SCHEDULE_DEFAULT_TAG
    defaults["date"] = section.get("date", fallback=SCHEDULE_DEFAULT_DATE).strip() or SCHEDULE_DEFAULT_DATE
    defaults["run_on_start"] = _str_to_bool(section.get("run_on_start", fallback="0"), default=False)
    try:
        defaults["catchup_minutes"] = max(
            0,
            int(section.get("catchup_minutes", fallback=str(SCHEDULE_CATCHUP_MINUTES))),
        )
    except ValueError:
        pass
    return defaults


def parse_clock(value: str) -> tuple[int, int]:
    """把 10:00 / 10 / 10点 / 10:00:30 解析成 (小时, 分钟)，秒一律忽略。"""
    text = (
        str(value or "")
        .strip()
        .replace("点", ":")
        .replace("时", ":")
        .replace("分", "")
        .replace("：", ":")
    )
    parts = [part for part in text.split(":") if part != ""]
    if not parts:
        raise ValueError("定时时间不能为空（支持 HH:MM，如 10:00）")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except ValueError as exc:
        raise ValueError(f"定时时间格式不正确（支持 HH:MM，如 10:00）：{value}") from exc
    if not 0 <= hour <= 23:
        raise ValueError(f"小时需在 0-23 之间：{value}")
    if not 0 <= minute <= 59:
        raise ValueError(f"分钟需在 0-59 之间：{value}")
    return hour, minute


def next_run_at(
    hour: int, minute: int, now: datetime | None = None
) -> datetime:
    """下一次触发时刻：今天该时刻已过就顺延到明天同一时刻。"""
    current = now or datetime.now()
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return target


def setup_scheduler_logger(*, console: bool = True, verbose: bool = False) -> logging.Logger:
    """常驻调度器的独立日志 data/logs/daily_monitor_schedule.log。

    与按日期拆分的跑批日志分开写：调度器跨天常驻，单文件便于一次看全流程。
    """
    logger = logging.getLogger("daily_monitor.schedule")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = LOG_ROOT / f"{SCHEDULE_LOG_NAME}.log"
    if not any(
        isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == log_path
        for handler in logger.handlers
    ):
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(file_handler)
    if console and not any(
        getattr(handler, "_scheduler_console", False) for handler in logger.handlers
    ):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler._scheduler_console = True
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console_handler)
    return logger


class DailyMonitorScheduler:
    """每天固定时刻跑一次 DailyMonitorRunner 的常驻调度器。

    只用标准库实现（datetime + time.sleep），不引三方依赖。要点：
        * 触发点由 next_run_at 计算，跑完再重算下一次，天然避免同日重复执行
        * 系统休眠/卡顿导致错过触发点时：超窗（catchup_minutes）内立即补跑一次，
          超过窗口则跳过并顺延，不会恢复后连续追跑把机器压死
        * 每次执行都新建一个 runner：日志文件按取数日期新鲜生成
        * stop() 与 Ctrl+C 都能干净退出，不会把 SSH 隧道留在半拉状态
    """

    def __init__(
        self,
        *,
        clock: str = SCHEDULE_DEFAULT_TIME,
        tag: str = SCHEDULE_DEFAULT_TAG,
        date_spec: str = SCHEDULE_DEFAULT_DATE,
        catchup_minutes: int = SCHEDULE_CATCHUP_MINUTES,
        run_on_start: bool = False,
        dry_run: bool = False,
        max_runs: int | None = None,
        poll_seconds: float = SCHEDULE_POLL_SECONDS,
        summary_path: str | Path | None = None,
        runner_kwargs: dict | None = None,
        runner_factory: Callable[[str, logging.Logger], DailyMonitorRunner] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._hour, self._minute = parse_clock(clock)
        self.clock = f"{self._hour:02d}:{self._minute:02d}"
        self.tag = tag
        self.date_spec = date_spec
        self.catchup_minutes = max(0, int(catchup_minutes))
        self.run_on_start = bool(run_on_start)
        self.dry_run = bool(dry_run)
        self.max_runs = max_runs
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.summary_path = Path(summary_path) if summary_path else None
        self.runner_kwargs = dict(runner_kwargs or {})
        self.runner_factory = runner_factory
        self._logger = logger
        self._stop = threading.Event()
        self.runs = 0
        self.failures = 0
        self.last_summary: dict | None = None

    # ---------- 对外 API ----------

    @property
    def logger(self) -> logging.Logger:
        if self._logger is None:
            self._logger = setup_scheduler_logger()
        return self._logger

    def stop(self) -> None:
        """请求调度器在下一次唤醒时退出（线程安全，可外部调用）。"""
        self._stop.set()

    def run_forever(self) -> int:
        """阻塞式常驻调度，直到 stop() 或 Ctrl+C；返回失败次数决定进程退出码。"""
        log = self.logger
        target = next_run_at(self._hour, self._minute)
        log.info("=" * 60)
        log.info(
            "定时任务已启动 | 每天 %s | 标签=%s | 取数日期=%s | 补跑窗口=%d 分钟",
            self.clock,
            self.tag,
            self.date_spec,
            self.catchup_minutes,
        )
        log.info("下一次触发：%s", target.strftime("%Y-%m-%d %H:%M:%S"))

        try:
            if self.run_on_start:
                log.info("启动即执行：先跑一次，跑完再按时钟对齐下一次")
                self.execute_once()
                target = next_run_at(self._hour, self._minute)
                log.info("下次触发：%s", target.strftime("%Y-%m-%d %H:%M:%S"))
            if self._done():
                return 1 if self.failures else 0
            while not self._stop.is_set():
                state = self._wait_until(target)
                if state == "stopped":
                    break
                if state == "missed":
                    log.warning(
                        "已过触发点且超出补跑窗口（%d 分钟），跳过本次并顺延",
                        self.catchup_minutes,
                    )
                    target = next_run_at(self._hour, self._minute)
                    continue
                log.info("-" * 60)
                log.info("定时触发：%s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                self.execute_once()
                if self._done():
                    break
                target = next_run_at(self._hour, self._minute)
                log.info("下次触发：%s", target.strftime("%Y-%m-%d %H:%M:%S"))
        except KeyboardInterrupt:
            log.warning("收到中断信号，调度器退出")
            return 130
        finally:
            log.info("定时任务已停止 | 共执行 %d 次，失败 %d 次", self.runs, self.failures)
        return 1 if self.failures else 0

    def execute_once(self, date_spec: str | None = None) -> dict | None:
        """按当前配置跑一次完整流程并返回汇总字典。"""
        log = self.logger
        spec = date_spec or self.date_spec
        try:
            target_date = normalize_date(spec)
        except ValueError as exc:
            self.failures += 1
            log.error("日期参数非法，跳过本次执行：%s", exc)
            return None

        runner = self._build_runner(target_date)
        self.runs += 1
        try:
            summary = runner.run(spec, dry_run=self.dry_run)
        except KeyboardInterrupt:
            runner.close_tunnels()
            raise
        except Exception as exc:  # noqa: BLE001 - 单次失败不能拖垮常驻调度
            self.failures += 1
            runner.close_tunnels()
            runner.logger.exception("定时执行异常终止")
            log.error("本次执行失败：%s", exc)
            return None
        if int(summary.get("failed", 0)):
            self.failures += 1
        self.last_summary = summary
        log.info(
            "本次完成 | 日期=%s | 共 %d 台，成功 %d，失败 %d，跳过 %d，耗时 %.2f 秒",
            summary.get("date", target_date),
            summary.get("total", 0),
            summary.get("success", 0),
            summary.get("failed", 0),
            summary.get("skipped", 0),
            summary.get("elapsed_seconds", 0),
        )
        self._write_summary(summary)
        return summary

    # ---------- 内部实现 ----------

    def _build_runner(self, target_date: str) -> DailyMonitorRunner:
        """每次执行都新建 runner，保证日志按取数日期生成。"""
        if self.runner_factory is not None:
            return self.runner_factory(target_date, self.logger)
        kwargs = dict(self.runner_kwargs)
        kwargs.setdefault("logger", setup_logger(target_date))
        return DailyMonitorRunner(**kwargs)

    def _wait_until(self, target: datetime) -> str:
        """睡到 target 为止；返回 due（准时/补跑）/ missed（超窗）/ stopped（被叫停）。"""
        while True:
            if self._stop.is_set():
                return "stopped"
            remaining = (target - datetime.now()).total_seconds()
            if remaining <= 0:
                if -remaining > self.catchup_minutes * 60:
                    return "missed"
                return "due"
            time.sleep(min(self.poll_seconds, remaining))

    def _done(self) -> bool:
        return self.max_runs is not None and self.runs >= max(1, self.max_runs)

    def _write_summary(self, summary: dict) -> None:
        if not self.summary_path:
            return
        try:
            self.summary_path.parent.mkdir(parents=True, exist_ok=True)
            self.summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:  # 落盘失败只影响旁证文件，不影响主流程
            self.logger.warning("汇总 JSON 写入失败：%s", exc)

# ---------- 命令行 ----------

def _discover_tags(queue_file: str | Path = QUEUE_FILE) -> list[str]:
    """从队列文件动态读取所有非空 tag 标签，用于 argparse 帮助文案。"""
    try:
        from services.device_repository import load_devices

        devices = load_devices(queue_file)
    except Exception:  # noqa: BLE001 - 解析失败时仍允许脚本运行
        return []
    return sorted({device.tag.strip() for device in devices if (device.tag or "").strip()})


def find_orphan_tunnels() -> list[tuple[int, str]]:
    """找出仍在运行的 ssh.exe 隧道进程（上次跑到一半被强杀留下的孤儿进程）。

    正常结束时 `close_tunnels()` 会清理干净；只有进程被强杀（任务管理器、IDE
    强制停止、系统崩溃）跳过了 finally 才会残留 —— Windows 上子进程不会随父进程
    退出而销毁。只匹配带端口转发(-L)的隧道，避免误伤用户自己开的普通 ssh 会话。
    """
    try:
        import psutil
    except ImportError:
        return []
    found: list[tuple[int, str]] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = proc.info["name"]
            if not name or name.lower() != "ssh.exe":
                continue
            command = " ".join(proc.info["cmdline"] or [])
            if "-L" in command:  # -L 即本地端口转发，是隧道的标志
                found.append((int(proc.info["pid"]), command))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def kill_orphan_tunnels() -> list[int]:
    """强杀残留的隧道进程并返回被清理的 pid 列表。"""
    try:
        import psutil
    except ImportError:
        return []
    killed: list[int] = []
    for pid, _command in find_orphan_tunnels():
        try:
            psutil.Process(pid).kill()
            killed.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


def disconnect_stale_adb(*, hard: bool = False) -> tuple[list[str], bool]:
    """清掉 adb devices 里非 device 状态的残留连接（offline / unauthorized）。

    adb disconnect 对 offline 条目常常无效（server 仍缓存着），hard=True 时
    退而用 `adb kill-server` 整体重启 adb —— 会断开本机所有 adb 连接，慎用。
    返回 (清理的连接列表, 是否执行了 kill-server)。
    """
    def _devices() -> list[str]:
        try:
            result = subprocess.run(
                [str(ADB_PATH), "devices"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return [
            line.split("\t")[0].strip()
            for line in (result.stdout or "").splitlines()[1:]
            if line.strip() and "\t" in line and "\tdevice" not in line
        ]

    stale = _devices()
    for serial in stale:
        try:
            subprocess.run(
                [str(ADB_PATH), "disconnect", serial],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue

    killed_server = False
    if hard and _devices():  # disconnect 没能真正清掉，重启 adb server
        try:
            subprocess.run(
                [str(ADB_PATH), "kill-server"],
                capture_output=True,
                timeout=30,
            )
            killed_server = True
        except (OSError, subprocess.SubprocessError):
            pass
    return stale, killed_server


def run_scheduled_task(args: argparse.Namespace) -> int:
    """schedule 子命令：拉起常驻调度器，每天固定时刻跑一次。

    优先级：命令行 > config/ui_settings.ini 的 [schedule] 段 > 内置默认值。
    """
    settings = load_schedule_settings()
    if not settings["enabled"]:
        print(
            "定时任务已关闭：config/ui_settings.ini 的 [schedule] enabled=0，"
            "改为 1 或用 --at 等参数覆盖后重试"
        )
        return 0

    clock_text = args.at or settings["time"]
    try:
        hour, minute = parse_clock(clock_text)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    tag = args.tag or settings["tag"]
    date_spec = args.date or settings["date"]
    # 校验标签：xlsx 里找不到直接报错，列出可用项。
    available_tags = _discover_tags(args.queue_file)
    if available_tags and tag.strip() not in available_tags:
        print(
            f"错误：标签「{tag}」在队列文件里不存在。可用标签：{', '.join(available_tags)}",
            file=sys.stderr,
        )
        return 2

    scheduler = DailyMonitorScheduler(
        clock=f"{hour:02d}:{minute:02d}",
        tag=tag,
        date_spec=date_spec,
        catchup_minutes=(
            args.catchup_minutes if args.catchup_minutes is not None else settings["catchup_minutes"]
        ),
        run_on_start=args.run_on_start or settings["run_on_start"],
        dry_run=args.dry_run,
        max_runs=1 if args.once else None,
        summary_path=args.summary_json,
        runner_kwargs={
            "queue_file": args.queue_file,
            "output_root": args.output_root,
            "tag": tag,
            "include_disabled": args.include_disabled,
            "tunnel_wait": args.tunnel_wait,
            "keep_tunnels": args.keep_tunnels,
            "limit": args.limit,
            "remote_dir": args.remote_dir,
            "remote_ext": args.remote_ext,
        },
        logger=setup_scheduler_logger(verbose=args.verbose),
    )
    print(
        f"定时任务已启动：每天 {scheduler.clock} 拉取「{tag}」（取数 {date_spec}），Ctrl+C 退出"
    )
    return scheduler.run_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daily_monitor",
        description="按标签批量拉取云手机上的日期 JSON（直连 SSH 跳板机，不涉及 VPN）",
    )
    sub = parser.add_subparsers(dest="command", metavar="{run,cleanup,schedule}")

    run_p = sub.add_parser("run", help="完整流程：读队列 → 建隧道 → 拉取 → 收尾（默认命令，可省略 run）")
    run_p.add_argument("--date", default="yesterday", help="数据日期：today / yesterday / YYYY-MM-DD，默认 yesterday（10 点拉取前一天日报）")
    run_p.add_argument(
        "--tag",
        default=DEFAULT_TAG,
        help=(
            f"队列标签筛选，默认 {DEFAULT_TAG}"
            + (f"；xlsx 里的标签：{', '.join(_discover_tags())}" if _discover_tags() else "")
        ),
    )
    run_p.add_argument("--queue-file", default=str(QUEUE_FILE), help="设备队列文件（xlsx/csv）")
    run_p.add_argument("--output-root", default=str(OUTPUT_ROOT), help="本地落盘根目录，默认 SF_json")
    run_p.add_argument("--tunnel-wait", type=float, default=DEFAULT_TUNNEL_WAIT, help="隧道建立等待秒数")
    run_p.add_argument("--keep-tunnels", action="store_true", help="结束后保留隧道不关闭")
    run_p.add_argument("--include-disabled", action="store_true", help="包含队列里停用的设备")
    run_p.add_argument("--dry-run", action="store_true", help="只打印计划，不连接不拉取")
    run_p.add_argument("--limit", type=int, help="只处理前 N 台设备，便于先验证单台")
    run_p.add_argument(
        "--remote-dir",
        default=REMOTE_DIR_TEMPLATE,
        help="远端目录模板，支持 {package_name} 占位，默认 %(default)s",
    )
    run_p.add_argument("--remote-ext", default=DEFAULT_EXTENSION, help="数据文件扩展名，默认 %(default)s")
    run_p.add_argument("--summary-json", help="把汇总结果额外写成 JSON 文件")
    run_p.add_argument(
        "--verbose",
        action="store_true",
        help="日志文件记录 DEBUG 中间态（远端路径、端口重分配等），排查问题时使用",
    )
    sub.add_parser(
        "cleanup",
        help="清理上次被强杀残留的 SSH 隧道孤儿进程与 offline adb 连接",
    ).add_argument(
        "--hard",
        action="store_true",
        help="adb disconnect 无效时用 adb kill-server 兜底（会断开本机所有 adb 连接）",
    )

    sched_p = sub.add_parser(
        "schedule",
        help="常驻定时任务：每天固定时刻拉取一次（默认 10:00 / 标签 重点私服）",
    )
    sched_p.add_argument(
        "--at",
        default=None,
        help="每日触发时刻 HH:MM，默认 10:00（可用 ini [schedule] time 覆盖）",
    )
    sched_p.add_argument(
        "--tag",
        default=None,
        help=f"拉取标签，默认 {SCHEDULE_DEFAULT_TAG}（可用 ini [schedule] tag 覆盖）",
    )
    sched_p.add_argument(
        "--date",
        default=None,
        help="每次跑批的取数日期：today / yesterday / YYYY-MM-DD，默认 yesterday（可用 ini [schedule] date 覆盖）",
    )
    sched_p.add_argument("--run-on-start", action="store_true", help="启动时先立即跑一次，之后按时刻等待")
    sched_p.add_argument("--once", action="store_true", help="只跑到下一个触发点执行一次就退出（便于验证）")
    sched_p.add_argument(
        "--catchup-minutes",
        type=int,
        default=None,
        help=f"错过触发点后仍允许补跑的分钟数，默认 {SCHEDULE_CATCHUP_MINUTES}",
    )
    sched_p.add_argument("--queue-file", default=str(QUEUE_FILE), help="设备队列文件（xlsx/csv）")
    sched_p.add_argument("--output-root", default=str(OUTPUT_ROOT), help="本地落盘根目录，默认 SF_json")
    sched_p.add_argument("--tunnel-wait", type=float, default=DEFAULT_TUNNEL_WAIT, help="隧道建立等待秒数")
    sched_p.add_argument("--keep-tunnels", action="store_true", help="每次跑完保留隧道不关闭")
    sched_p.add_argument("--include-disabled", action="store_true", help="包含队列里停用的设备")
    sched_p.add_argument("--dry-run", action="store_true", help="每次触发只打印计划，不连接不拉取")
    sched_p.add_argument("--limit", type=int, help="只处理前 N 台设备，便于先验证单台")
    sched_p.add_argument(
        "--remote-dir",
        default=REMOTE_DIR_TEMPLATE,
        help="远端目录模板，支持 {package_name} 占位，默认 %(default)s",
    )
    sched_p.add_argument("--remote-ext", default=DEFAULT_EXTENSION, help="数据文件扩展名，默认 %(default)s")
    sched_p.add_argument("--summary-json", help="每次跑完把汇总结果写成 JSON 文件")
    sched_p.add_argument("--verbose", action="store_true", help="日志同时记录 DEBUG 中间态")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console_encoding()
    tokens = list(sys.argv[1:] if argv is None else argv)
    # 允许省略 run：python tools/daily_monitor.py --date 2026-10-16 等价于 ... run --date ...
    if not tokens or (tokens[0].startswith("-") and tokens[0] not in ("-h", "--help")):
        tokens = ["run", *tokens]
    args = build_parser().parse_args(tokens)

    if args.command == "cleanup":
        orphans = find_orphan_tunnels()
        if not orphans:
            print("没有发现残留的 SSH 隧道进程 ✓")
        else:
            print(f"发现 {len(orphans)} 个残留隧道进程：{[pid for pid, _ in orphans]}")
            killed = kill_orphan_tunnels()
            print(f"已清理 {len(killed)} 个：{killed}")
        stale, killed_server = disconnect_stale_adb(hard=args.hard)
        print(f"清理 offline/unauthorized adb 连接 {len(stale)} 条：{stale or '无'}")
        if killed_server:
            print("adb disconnect 未能清除，已执行 adb kill-server 重启 adb")
        if stale and not killed_server:
            remaining = disconnect_stale_adb()[0]
            if remaining:
                print(f"仍有 {len(remaining)} 条 offline 条目（adb server 缓存），加 --hard 可重启 adb")
        return 0

    if args.command == "schedule":
        return run_scheduled_task(args)

    try:
        target_date = normalize_date(args.date)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    # 校验 --tag：xlsx 里找不到直接报错，列出可用项。
    available_tags = _discover_tags(args.queue_file)
    if available_tags and args.tag.strip() not in available_tags:
        print(
            f"错误：标签「{args.tag}」在队列文件里不存在。可用标签：{', '.join(available_tags)}",
            file=sys.stderr,
        )
        return 2
    runner = DailyMonitorRunner(
        queue_file=args.queue_file,
        output_root=args.output_root,
        tag=args.tag,
        include_disabled=args.include_disabled,
        tunnel_wait=args.tunnel_wait,
        keep_tunnels=args.keep_tunnels,
        limit=args.limit,
        remote_dir=args.remote_dir,
        remote_ext=args.remote_ext,
        logger=setup_logger(target_date, verbose=args.verbose),
    )
    try:
        summary = runner.run(args.date, dry_run=args.dry_run)
    except KeyboardInterrupt:
        runner.logger.warning("收到中断信号，正在释放隧道")
        runner.close_tunnels()
        return 130
    except Exception:
        runner.close_tunnels()
        runner.logger.exception("任务异常终止")
        raise
    if args.summary_json:
        Path(args.summary_json).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

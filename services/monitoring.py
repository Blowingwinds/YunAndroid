"""Configuration and alert policy for the daily JSON monitor."""

from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "monitor.ini"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class MonitorConfig:
    config_path: Path
    queue_file: Path
    state_db: Path
    poll_interval_seconds: int
    stale_threshold_seconds: int
    missing_grace_seconds: int
    max_workers: int
    control_host: str
    control_port: int
    control_token: str
    feishu_webhook_url: str
    feishu_security_keyword: str
    feishu_secret: str


@dataclass(frozen=True)
class Observation:
    device_key: str
    device_id: str
    app_name: str
    group: str
    remote_address: str
    status: str
    mtime_epoch: int | None = None
    size: int | None = None
    device_epoch: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class AlertResult:
    observation: Observation
    status: str
    is_alert: bool
    reason: str
    age_seconds: int | None = None

    @property
    def device_key(self) -> str:
        return self.observation.device_key


def _positive_int(parser: ConfigParser, section: str, key: str, fallback: int) -> int:
    try:
        value = parser.getint(section, key, fallback=fallback)
    except ValueError as exc:
        raise ValueError(f"配置项 [{section}] {key} 必须是整数") from exc
    if value <= 0:
        raise ValueError(f"配置项 [{section}] {key} 必须大于 0")
    return value


def _resolve_config_path(config_dir: Path, value: str, fallback: Path) -> Path:
    text = str(value).strip()
    path = Path(text) if text else fallback
    if not path.is_absolute():
        path = config_dir / path
    return path.resolve()


def load_monitor_config(path: str | Path = DEFAULT_CONFIG_PATH) -> MonitorConfig:
    """Load monitor and Feishu settings from one local INI file."""
    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"监控配置文件不存在：{config_path}")

    parser = ConfigParser(interpolation=None)
    try:
        with config_path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, ValueError) as exc:
        raise ValueError(f"读取监控配置失败：{exc}") from exc

    poll_minutes = _positive_int(parser, "monitor", "poll_interval_minutes", 30)
    stale_minutes = _positive_int(parser, "monitor", "stale_threshold_minutes", 60)
    grace_minutes = _positive_int(parser, "monitor", "missing_grace_minutes", 90)
    max_workers = _positive_int(parser, "monitor", "max_workers", 6)
    if max_workers > 16:
        raise ValueError("配置项 [monitor] max_workers 不能超过 16")
    control_port = _positive_int(parser, "monitor", "control_port", 18765)
    if control_port > 65535:
        raise ValueError("配置项 [monitor] control_port 不能超过 65535")

    control_host = parser.get("monitor", "control_host", fallback="127.0.0.1").strip()
    if control_host != "127.0.0.1":
        raise ValueError("监控控制接口只能绑定 127.0.0.1")

    config_dir = config_path.parent
    queue_file = _resolve_config_path(
        config_dir,
        parser.get("paths", "queue_file", fallback="cloud_phone_queue.xlsx"),
        config_dir / "cloud_phone_queue.xlsx",
    )
    state_db = _resolve_config_path(
        config_dir,
        parser.get("paths", "state_db", fallback="../data/monitor/monitor.sqlite3"),
        PROJECT_ROOT / "data" / "monitor" / "monitor.sqlite3",
    )
    feishu_security_keyword = parser.get(
        "feishu", "security_keyword", fallback=""
    ).strip()
    if not feishu_security_keyword:
        raise ValueError("配置项 [feishu] security_keyword 不能为空")

    return MonitorConfig(
        config_path=config_path,
        queue_file=queue_file,
        state_db=state_db,
        poll_interval_seconds=poll_minutes * 60,
        stale_threshold_seconds=stale_minutes * 60,
        missing_grace_seconds=grace_minutes * 60,
        max_workers=max_workers,
        control_host=control_host,
        control_port=control_port,
        control_token=parser.get("monitor", "control_token", fallback="").strip(),
        feishu_webhook_url=parser.get(
            "feishu",
            "alert_webhook_url",
            fallback=parser.get("feishu", "webhook_url", fallback=""),
        ).strip(),
        feishu_security_keyword=feishu_security_keyword,
        feishu_secret=parser.get("feishu", "secret", fallback="").strip(),
    )


def evaluate_observation(
    observation: Observation,
    now: datetime,
    config: MonitorConfig,
) -> AlertResult:
    """Convert one raw device observation into a monitor state."""
    moment = now.astimezone(BEIJING_TZ)
    if observation.status == "missing":
        seconds_since_midnight = moment.hour * 3600 + moment.minute * 60 + moment.second
        if seconds_since_midnight <= config.missing_grace_seconds:
            return AlertResult(observation, "waiting", False, "等待当天文件生成")
        return AlertResult(observation, "missing", True, "当天 JSON 文件不存在")

    if observation.status != "ok":
        reason = observation.detail or "设备检查失败"
        return AlertResult(observation, observation.status, True, reason)

    now_epoch = int(moment.timestamp())
    if observation.device_epoch is not None and abs(observation.device_epoch - now_epoch) > 300:
        offset = observation.device_epoch - now_epoch
        return AlertResult(
            observation,
            "clock_skew",
            True,
            f"设备时间与北京时间相差 {offset} 秒",
        )
    if observation.mtime_epoch is None:
        return AlertResult(observation, "unreachable", True, "未取得文件修改时间")

    age_seconds = now_epoch - observation.mtime_epoch
    if age_seconds < -300:
        return AlertResult(
            observation,
            "clock_skew",
            True,
            f"文件修改时间来自未来 {-age_seconds} 秒",
            age_seconds,
        )
    if age_seconds > config.stale_threshold_seconds:
        return AlertResult(
            observation,
            "stale",
            True,
            f"当天 JSON 已停更 {_format_duration(age_seconds)}",
            age_seconds,
        )
    return AlertResult(observation, "normal", False, "正常", age_seconds)


def _format_duration(seconds: int) -> str:
    minutes = max(0, int(seconds)) // 60
    hours, remaining = divmod(minutes, 60)
    return f"{hours} 小时 {remaining} 分钟" if hours else f"{remaining} 分钟"


def build_alert_message(
    *,
    now: datetime,
    total: int,
    security_keyword: str,
    new_alerts: list[AlertResult],
) -> str:
    """Build one text message containing alerts first reported in this round."""
    moment = now.astimezone(BEIJING_TZ)
    lines = [
        f"From : {security_keyword}",
        "【云手机服务器 JSON 监控告警】",
        f"检查时间：{moment.strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        f"设备总数：{total}",
        f"本轮告警：{len(new_alerts)}",
        "",
    ]
    titles = {
        "stale": "停更",
        "missing": "文件缺失",
        "unreachable": "无法检测",
        "config_error": "配置异常",
        "clock_skew": "时钟异常",
    }
    for index, result in enumerate(new_alerts, 1):
        item = result.observation
        state = titles.get(result.status, result.status)
        identity = " / ".join(value for value in (item.app_name, item.group) if value) or item.device_id
        lines.append(
            f"{index}. [{state}] {identity} | {item.remote_address} | {result.reason}"
        )
    return "\n".join(lines).rstrip()


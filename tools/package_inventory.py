"""List third-party Android packages across a range of cloud phones.

For each requested ``10.237.0.X`` address this tool opens an SSH local
forward, connects ADB to the forwarded port, and reads package/application
labels.  The command runner and tunnel lifecycle callbacks are injectable so
the workflow can be tested without SSH, ADB, or a cloud phone.
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADB_PATH = PROJECT_ROOT / "tools" / "scrcpy-win64-v4.0" / "adb.exe"
DEFAULT_START_LOCAL_PORT = 57600
DEFAULT_REMOTE_PREFIX = "10.237.0."
DEFAULT_REMOTE_PORT = 5555
INVENTORY_FIELDNAMES = ["remote_ip", "local_port", "package_name", "app_name"]
DEFAULT_EXCLUDED_PACKAGES = frozenset({"com.netease.nie.yosemite"})


try:
    from services.openSSH import (
        build_ssh_command,
        pem_path as DEFAULT_PEM_PATH,
        replace_ssh_host,
        ssh_user_host as DEFAULT_SSH_USER_HOST,
        start_ssh_tunnel,
        stop_ssh_process,
    )
except ModuleNotFoundError:  # Allows ``python tools/package_inventory.py``.
    sys.path.insert(0, str(PROJECT_ROOT))
    from services.openSSH import (  # type: ignore[no-redef]
        build_ssh_command,
        pem_path as DEFAULT_PEM_PATH,
        replace_ssh_host,
        ssh_user_host as DEFAULT_SSH_USER_HOST,
        start_ssh_tunnel,
        stop_ssh_process,
    )


_LABEL_PATTERN = re.compile(
    r"^(?:application-label(?:-[^:=]+)?|nonLocalizedLabel|label)\s*[:=]\s*(.*?)\s*$",
    re.IGNORECASE,
)
_EMBEDDED_LABEL_PATTERN = re.compile(
    r"(?:nonLocalizedLabel|application-label(?:-[^:=]+)?)\s*[:=]\s*(.*?)(?=\s+(?:packageName|className|uid|sourceDir|publicSourceDir|dataDir|labelRes|icon|flags)\s*=|[}\n]|$)",
    re.IGNORECASE,
)


def parse_ip_range(expression: str | int) -> list[int]:
    """Parse one or more last-octet values, such as ``2`` or ``2-100``.

    Comma-separated values are accepted as a convenience (``2, 5-7``), and
    values are returned in input order.  Cloud-phone addresses use host
    octets 1 through 254; malformed, descending, or duplicate values are
    rejected to keep the resulting queue deterministic.
    """

    text = str(expression).strip()
    if not text:
        raise ValueError("IP 范围不能为空")

    values: list[int] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            raise ValueError(f"IP 范围格式错误: {expression}")
        parts = [part.strip() for part in token.split("-")]
        if len(parts) > 2 or any(not part.isdigit() for part in parts):
            raise ValueError(f"IP 范围格式错误: {token}")
        start = int(parts[0])
        end = int(parts[-1])
        if len(parts) == 2 and end < start:
            raise ValueError(f"IP 范围必须从小到大: {token}")
        if start < 1 or end > 254:
            raise ValueError(f"IP 末段必须在 1-254 范围内: {token}")
        values.extend(range(start, end + 1))

    if len(set(values)) != len(values):
        raise ValueError("IP 范围不能包含重复地址")
    return values


def build_tunnel_mappings(
    ip_range: str | int | Iterable[int],
    start_local_port: int = DEFAULT_START_LOCAL_PORT,
    remote_prefix: str = DEFAULT_REMOTE_PREFIX,
    remote_port: int = DEFAULT_REMOTE_PORT,
) -> list[dict[str, int | str]]:
    """Build sequential local-to-remote mappings for the requested hosts."""

    if isinstance(ip_range, str) or isinstance(ip_range, int):
        ip_values = parse_ip_range(ip_range)
    else:
        ip_values = list(ip_range)
        if not ip_values:
            raise ValueError("IP 范围不能为空")
        if any(not isinstance(value, int) or not 1 <= value <= 254 for value in ip_values):
            raise ValueError("IP 末段必须在 1-254 范围内")
        if len(set(ip_values)) != len(ip_values):
            raise ValueError("IP 范围不能包含重复地址")

    try:
        local_port = int(start_local_port)
        target_remote_port = int(remote_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("端口必须是整数") from exc
    if not 1 <= local_port <= 65535:
        raise ValueError(f"本地起始端口超出范围: {local_port}")
    if not 1 <= target_remote_port <= 65535:
        raise ValueError(f"远端端口超出范围: {target_remote_port}")
    if local_port + len(ip_values) - 1 > 65535:
        raise ValueError("本地端口范围超出 65535")
    if not remote_prefix or not remote_prefix.endswith("."):
        raise ValueError("远端 IP 前缀必须以 '.' 结尾")

    mappings: list[dict[str, int | str]] = []
    for index, last_octet in enumerate(ip_values):
        assigned_port = local_port + index
        mappings.append(
            {
                "remote_ip": f"{remote_prefix}{last_octet}",
                "remote_port": target_remote_port,
                "local_port": assigned_port,
                "serial": f"127.0.0.1:{assigned_port}",
            }
        )
    return mappings


def parse_third_party_packages(output: str, exclude_prefix: str = "net") -> list[str]:
    """Extract unique, relevant package names from third-party package output."""

    packages: list[str] = []
    seen: set[str] = set()
    prefix = exclude_prefix.lower()
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("package:"):
            continue
        package = line[len("package:") :].strip()
        if (
            not package
            or package.lower().startswith(prefix)
            or package.casefold() in DEFAULT_EXCLUDED_PACKAGES
            or package in seen
        ):
            continue
        seen.add(package)
        packages.append(package)
    return packages


def extract_application_name(output: str, package_name: str) -> str:
    """Read an application label from ``dumpsys package`` output.

    Android versions differ in whether the label is quoted and whether a
    locale suffix is present.  If no label is reported, return an empty string
    so the package name is not mistakenly displayed as the application name.
    """

    def clean_label(value: str) -> str:
        label = value.strip().strip("'\"").strip()
        if not label or label.lower() in {"null", "none"}:
            return ""
        if label.startswith(("@", "0x")):
            return ""
        return label

    for raw_line in (output or "").splitlines():
        match = _LABEL_PATTERN.match(raw_line.strip())
        if not match:
            continue
        label = clean_label(match.group(1))
        if label:
            return label

    for match in _EMBEDDED_LABEL_PATTERN.finditer(output or ""):
        label = clean_label(match.group(1))
        if label:
            return label
    return ""


class PackageInventory:
    """Collect package/application rows while owning each temporary tunnel."""

    def __init__(
        self,
        adb_path: str | Path = DEFAULT_ADB_PATH,
        ssh_user_host: str = DEFAULT_SSH_USER_HOST,
        pem_path: str | Path = DEFAULT_PEM_PATH,
        tunnel_wait: float = 3.0,
        runner: Callable = subprocess.run,
        start_tunnel: Callable = start_ssh_tunnel,
        stop_tunnel: Callable = stop_ssh_process,
        sleeper: Callable = time.sleep,
        remote_prefix: str = DEFAULT_REMOTE_PREFIX,
        remote_port: int = DEFAULT_REMOTE_PORT,
    ):
        self.adb_path = str(adb_path)
        self.ssh_user_host = ssh_user_host
        self.pem_path = str(pem_path)
        self.tunnel_wait = tunnel_wait
        self.runner = runner
        self.start_tunnel = start_tunnel
        self.stop_tunnel = stop_tunnel
        self.sleeper = sleeper
        self.remote_prefix = remote_prefix
        self.remote_port = remote_port
        self.errors: list[dict[str, str | int]] = []

    def _run(self, command: Sequence[str], timeout: int = 300):
        try:
            result = self.runner(
                list(command),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"找不到执行文件: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"命令超时: {' '.join(command)}") from exc

        # A small adapter makes simple injected runners returning (stdout,
        # stderr) tuples usable as well as subprocess.CompletedProcess.
        if isinstance(result, tuple) and len(result) == 2:
            result = subprocess.CompletedProcess(list(command), 0, result[0], result[1])
        if getattr(result, "returncode", 0) != 0:
            output = (getattr(result, "stdout", "") or getattr(result, "stderr", "") or "").strip()
            raise RuntimeError(output or f"命令失败，退出码 {result.returncode}")
        return result

    def _adb(self, serial: str, *arguments: str, timeout: int = 300):
        return self._run([self.adb_path, "-s", serial, *arguments], timeout=timeout)

    def _connect_adb(self, serial: str):
        # adb connect intentionally omits -s because the endpoint is not yet
        # registered as an ADB device.
        result = self._run([self.adb_path, "connect", serial], timeout=30)
        output = ((getattr(result, "stdout", "") or "") + " " + (getattr(result, "stderr", "") or "")).lower()
        if "connected to" not in output and "already connected" not in output:
            raise RuntimeError(f"ADB 连接失败: {output.strip()}")

    def collect(
        self,
        ip_range: str | int | Iterable[int],
        start_local_port: int = DEFAULT_START_LOCAL_PORT,
    ) -> list[dict[str, str | int]]:
        """Return one row per non-``net`` third-party package."""

        self.errors = []
        mappings = build_tunnel_mappings(
            ip_range,
            start_local_port=start_local_port,
            remote_prefix=self.remote_prefix,
            remote_port=self.remote_port,
        )
        return self.collect_mappings(mappings)

    def collect_mappings(
        self,
        mappings: Iterable[dict[str, str | int]],
        reuse_serials: Iterable[str] | None = None,
    ) -> list[dict[str, str | int]]:
        """Scan explicit queue mappings, reusing serials already connected by the UI.

        Mappings whose serial is listed in ``reuse_serials`` are assumed to
        have an active SSH tunnel and ADB connection.  All other mappings get
        a temporary tunnel and ADB connection which are cleaned up when that
        mapping finishes.
        """

        self.errors = []
        records: list[dict[str, str | int]] = []
        reused = {str(serial) for serial in (reuse_serials or ())}

        for mapping in mappings:
            process = None
            local_port = int(mapping["local_port"])
            public_ip = str(mapping.get("public_ip", "")).strip()
            route_fields = {"public_ip": public_ip} if public_ip else {}
            serial = str(mapping.get("serial") or f"127.0.0.1:{local_port}")
            reused_connection = serial in reused
            try:
                if not reused_connection:
                    target = (
                        replace_ssh_host(self.ssh_user_host, public_ip)
                        if public_ip
                        else self.ssh_user_host
                    )
                    ssh_command = build_ssh_command(mapping, target, self.pem_path)
                    process = self.start_tunnel(ssh_command)
                    self.sleeper(self.tunnel_wait)
                    self._connect_adb(serial)
                package_result = self._adb(serial, "shell", "pm", "list", "packages", "-3", timeout=60)
                packages = parse_third_party_packages(getattr(package_result, "stdout", ""))
                for package in packages:
                    try:
                        label_result = self._adb(serial, "shell", "dumpsys", "package", package, timeout=60)
                        app_name = extract_application_name(getattr(label_result, "stdout", ""), package)
                    except RuntimeError as exc:
                        self.errors.append({**route_fields, "remote_ip": mapping["remote_ip"], "local_port": mapping["local_port"], "package_name": package, "error": str(exc)})
                        app_name = package
                    records.append(
                        {
                            **route_fields,
                            "remote_ip": mapping["remote_ip"],
                            "local_port": mapping["local_port"],
                            "package_name": package,
                            "app_name": app_name,
                        }
                    )
            except Exception as exc:
                self.errors.append(
                    {
                        **route_fields,
                        "remote_ip": mapping["remote_ip"],
                        "local_port": local_port,
                        "error": str(exc),
                    }
                )
            finally:
                if process is not None:
                    self.stop_tunnel(process)
                if not reused_connection:
                    try:
                        self._run([self.adb_path, "disconnect", serial], timeout=30)
                    except RuntimeError:
                        pass
        return records


def save_inventory_csv(records: Iterable[dict[str, str | int]], target: str | Path) -> Path:
    """Write inventory rows to a UTF-8 CSV file and return its path."""

    output_path = Path(target)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=INVENTORY_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    return output_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过 SSH/ADB 罗列云手机第三方应用")
    parser.add_argument("ip_range", help="远端 IP 末段，例如 2 或 2-100")
    parser.add_argument("--start-port", type=int, default=DEFAULT_START_LOCAL_PORT, help="本地起始转发端口，默认 57600")
    parser.add_argument("--output", type=Path, help="可选，将清单写入 CSV 文件")
    parser.add_argument("--adb", type=Path, default=DEFAULT_ADB_PATH, help="adb 可执行文件路径")
    parser.add_argument("--ssh-user-host", default=DEFAULT_SSH_USER_HOST)
    parser.add_argument("--pem", type=Path, default=DEFAULT_PEM_PATH)
    parser.add_argument("--tunnel-wait", type=float, default=3.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        inventory = PackageInventory(
            adb_path=args.adb,
            ssh_user_host=args.ssh_user_host,
            pem_path=args.pem,
            tunnel_wait=args.tunnel_wait,
        )
        records = inventory.collect(args.ip_range, start_local_port=args.start_port)
    except (ValueError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    print("远端 IP\t本地端口\t包名\t应用名")
    for record in records:
        print(f"{record['remote_ip']}\t{record['local_port']}\t{record['package_name']}\t{record['app_name']}")
    if args.output:
        save_inventory_csv(records, args.output)
        print(f"清单已写入: {args.output}")
    for error in inventory.errors:
        print(f"警告 {error.get('remote_ip')}:{error.get('local_port')}: {error['error']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

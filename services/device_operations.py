"""ADB, Airtest, screenshot, Lua, and scrcpy operations used by the UI."""

from __future__ import annotations

import re
import subprocess
import sys
import os
import ctypes
import shutil
import tempfile
from ctypes import wintypes
from datetime import datetime
from math import ceil
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit
from urllib.request import urlretrieve
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

from services.device_repository import Device


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = PROJECT_ROOT / "tools" / "scrcpy-win64-v4.0"
ADB_PATH = TOOLS_DIR / "adb.exe"
SCRCPY_PATH = TOOLS_DIR / "scrcpy.exe"
SCRCPY_LOG_DIR = PROJECT_ROOT / "data" / "logs" / "scrcpy"
AIRTEST_LOG_DIR = PROJECT_ROOT / "data" / "reports"
AIRTEST_LOGIN_SCRIPT_PATH = PROJECT_ROOT / "tools" / "airtest" / "airtest_login.py"
AIRTEST_AUTO_LOGIN_SCRIPT_PATH = PROJECT_ROOT / "tools" / "airtest" / "login_all_emulators.py"
AUTO_LOGIN_ACCOUNT = os.environ.get("YUNANDROID_LOGIN_ACCOUNT", "")
AUTO_LOGIN_PASSWORD = os.environ.get("YUNANDROID_LOGIN_PASSWORD", "")
_ANDROID_PATH_PART = re.compile(r"^[^/\\]+$")
_PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
_JSON_DATE_FILENAME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$", re.IGNORECASE)
LUA_REMOTE_FILENAME = "ChallengeLeaderMgr.luac"
LUA_DESTINATION_DEFAULT = "default"
LUA_DESTINATION_64BIT = "64bit"
LUA_TARGET_TEMPLATES = {
    LUA_DESTINATION_DEFAULT: (
        "/data/data/{package_name}/files/atmu/src/mgr/ChallengeLeaderMgr.luac"
    ),
    LUA_DESTINATION_64BIT: (
        "/data/data/{package_name}/files/atmu/src/64bit/mgr/ChallengeLeaderMgr.luac"
    ),
}
APK_REMOTE_DIR = "/data/local/tmp"
ARTIFACT_REMOTE_PATHS = {
    "sf.json": "/data/data/{package_name}/files/data/sf.json",
    "dist.lua": "/data/data/{package_name}/files/patch/dist.lua",
}
JSON_REMOTE_DIR = "/data/data/{package_name}/files/data"
_BACKGROUND_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def calculate_scrcpy_window_layout(
    device_count: int,
    columns: int,
    screen_geometry: tuple[int, int, int, int],
    top_margin: int = 24,
    bottom_margin: int = 24,
    window_aspect_ratio: tuple[int, int] = (16, 9),
) -> list[tuple[int, int, int, int]]:
    """Return one ``(x, y, width, height)`` rectangle per device in a grid.

    The margins reserve screen space above and below the grid. Tiles use a
    compact aspect-ratio-based height and shrink only when all required rows
    would otherwise exceed the usable screen height. The remaining pixels at
    the right or bottom edge are intentionally left unused.
    """
    if device_count < 0:
        raise ValueError("device_count must be non-negative")
    if columns < 1:
        raise ValueError("columns must be positive")
    if top_margin < 0 or bottom_margin < 0:
        raise ValueError("screen margins must be non-negative")
    try:
        aspect_width, aspect_height = window_aspect_ratio
    except (TypeError, ValueError) as exc:
        raise ValueError("window aspect ratio must contain width and height") from exc
    if aspect_width < 1 or aspect_height < 1:
        raise ValueError("window aspect ratio must be positive")

    screen_x, screen_y, screen_width, screen_height = screen_geometry
    if screen_width < 1 or screen_height < 1:
        raise ValueError("screen geometry must have positive dimensions")
    if top_margin + bottom_margin >= screen_height:
        raise ValueError("screen margins must leave usable screen height")
    if not device_count:
        return []

    rows = ceil(device_count / columns)
    usable_height = screen_height - top_margin - bottom_margin
    if columns > screen_width or rows > usable_height:
        raise ValueError("screen geometry is too small for the requested scrcpy grid")
    cell_width = screen_width // columns
    preferred_cell_height = max(1, cell_width * aspect_height // aspect_width)
    cell_height = min(usable_height // rows, preferred_cell_height)
    return [
        (
            screen_x + (index % columns) * cell_width,
            screen_y + top_margin + (index // columns) * cell_height,
            cell_width,
            cell_height,
        )
        for index in range(device_count)
    ]


def _apk_filename(url: str) -> str:
    filename = Path(unquote(urlparse(url).path)).name.strip()
    if not filename or not filename.lower().endswith(".apk"):
        raise ValueError("APK 下载链接必须指向 .apk 文件")
    return filename


def normalize_download_url(url: str) -> str:
    """Percent-encode Unicode characters in an HTTP URL path without double-encoding."""
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("APK 下载链接必须是有效的 HTTP(S) URL")
    encoded_path = quote(unquote(parsed.path), safe="/%:@-._~!$&'()*+,;=")
    return urlunsplit((parsed.scheme, parsed.netloc, encoded_path, parsed.query, parsed.fragment))


def extract_apk_app_name(url: str) -> str:
    """Extract the application name before the first underscore in an APK URL filename."""
    stem = Path(_apk_filename(url)).stem
    app_name = stem.split("_", 1)[0].strip()
    if not app_name:
        raise ValueError("APK 文件名中未找到应用名")
    return app_name


def extract_apk_package_name(url: str) -> str:
    """Extract the package-name candidate between the first and second underscores."""
    stem = Path(_apk_filename(url)).stem
    parts = stem.split("_")
    if len(parts) < 2:
        return ""
    candidate = parts[1].strip()
    try:
        return validate_package_name(candidate)
    except ValueError:
        return ""


def validate_lua_target(target: str) -> str:
    """Allow one absolute Android path without traversal segments."""
    normalized = target.strip()
    parts = normalized.split("/")
    if not normalized.startswith("/") or not normalized or ".." in parts:
        raise ValueError("Lua 目标路径必须是安全的绝对 Android 路径")
    if any(not _ANDROID_PATH_PART.match(part) for part in parts[1:] if part):
        raise ValueError("Lua 目标路径包含不支持的文件名")
    return normalized


def validate_remote_file_path(target: str) -> str:
    """Allow one safe absolute Android file path for a root ADB push."""
    normalized = str(target).strip()
    if not normalized.startswith("/") or not normalized or "\x00" in normalized:
        raise ValueError("远端目标路径必须是安全的绝对 Android 路径")
    parts = normalized.split("/")
    if ".." in parts or any(ord(char) < 32 for char in normalized):
        raise ValueError("远端目标路径包含不支持的字符")
    if any(not _ANDROID_PATH_PART.match(part) for part in parts[1:] if part):
        raise ValueError("远端目标路径包含不支持的文件名")
    return normalized


def validate_package_name(package_name: str) -> str:
    """Allow one Android application ID for constructing a data-directory path."""
    normalized = package_name.strip()
    if not _PACKAGE_NAME_PATTERN.fullmatch(normalized):
        raise ValueError("APK 包名必须是单个合法的 Android 包名")
    return normalized


def build_artifact_remote_path(artifact_type: str, package_name: str) -> str:
    """Build an SF artifact path from an Android package identifier."""
    artifact = str(artifact_type).strip()
    remote_template = ARTIFACT_REMOTE_PATHS.get(artifact)
    if remote_template is None:
        raise ValueError(f"不支持的拉取文件类型：{artifact}")
    safe_package_name = validate_package_name(package_name)
    return remote_template.format(package_name=safe_package_name)


def cleanup_json_pull_staging(root: str | Path) -> None:
    """Remove interrupted JSON pull staging directories below one output root."""
    root_path = Path(root)
    if not root_path.exists():
        return
    for path in root_path.rglob(".*.json-pull-*"):
        if not path.is_dir():
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            continue


def build_lua_target(
    package_name: str,
    destination: str = LUA_DESTINATION_DEFAULT,
) -> str:
    """Build an allowlisted Lua destination inside one application's data directory."""
    safe_package_name = validate_package_name(package_name)
    destination_key = str(destination).strip()
    template = LUA_TARGET_TEMPLATES.get(destination_key)
    if template is None:
        raise ValueError(f"不支持的 Lua 推送目标：{destination}")
    return validate_lua_target(template.format(package_name=safe_package_name))


class DeviceOperations:
    def __init__(
        self,
        adb_path: Path = ADB_PATH,
        scrcpy_path: Path = SCRCPY_PATH,
        runner: Callable = subprocess.run,
        popen: Callable = subprocess.Popen,
        downloader: Callable = urlretrieve,
    ):
        self.adb_path = Path(adb_path)
        self.scrcpy_path = Path(scrcpy_path)
        self.python_executable = sys.executable
        self.runner = runner
        self.popen = popen
        self.downloader = downloader
        self._scrcpy_window_layout: dict[str, tuple[int, int, int, int]] = {}
        self._scrcpy_processes: dict[str, object] = {}
        self._scrcpy_log_paths: dict[int, Path] = {}
        self._adb_shell_processes: dict[str, object] = {}
        self._last_scrcpy_arrangement: dict[str, tuple[int, int, int, int]] = {}

    def set_scrcpy_window_layout(
        self,
        layout: Mapping[str, tuple[int, int, int, int]] | None,
    ) -> None:
        """Configure optional window rectangles keyed by device serial."""
        self._scrcpy_window_layout = dict(layout or {})

    def adb_command(self, serial: str, *arguments: str) -> list[str]:
        return [str(self.adb_path), "-s", serial, *arguments]

    def _run(
        self,
        command: Sequence[str],
        timeout: int = 300,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> subprocess.CompletedProcess:
        try:
            runner_kwargs = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": timeout,
                "creationflags": _BACKGROUND_CREATION_FLAGS,
            }
            if env is not None:
                runner_kwargs["env"] = env
            if cwd is not None:
                runner_kwargs["cwd"] = str(cwd)
            result = self.runner(list(command), **runner_kwargs)
        except FileNotFoundError as exc:
            raise RuntimeError(f"找不到执行文件：{command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"命令超时：{' '.join(command)}") from exc
        if result.returncode != 0:
            output = "\n".join(
                part.strip()
                for part in (result.stdout or "", result.stderr or "")
                if part and part.strip()
            )
            raise RuntimeError(output or f"命令失败，退出码 {result.returncode}")
        return result

    def _ensure_adb_root(self, device: Device) -> None:
        result = self._run(self.adb_command(device.serial, "root"), timeout=60)
        output = f"{result.stdout or ''} {result.stderr or ''}".lower()
        if "cannot run as root" in output or "permission denied" in output:
            raise RuntimeError(f"设备不支持 root：{output.strip()}")

    def connect(self, device: Device) -> str:
        result = self._run([str(self.adb_path), "connect", device.serial], timeout=30)
        state = self._run(self.adb_command(device.serial, "get-state"), timeout=30)
        if (state.stdout or "").strip().lower() != "device":
            raise RuntimeError(f"设备状态异常：{state.stdout.strip()}")
        return (result.stdout or "connected").strip()

    def check_adb_ready(self, device: Device) -> str:
        """Return the ADB state when a device is ready for Scrcpy."""
        state = self._run(self.adb_command(device.serial, "get-state"), timeout=30)
        normalized = (state.stdout or "").strip().lower()
        if normalized != "device":
            raise RuntimeError(
                f"ADB state for {device.serial} is not ready: {normalized or 'unknown'}"
            )
        return normalized

    def install_apk(self, device: Device, apk_path: str | Path) -> str:
        path = Path(apk_path)
        if not path.is_file():
            raise FileNotFoundError(f"APK 文件不存在：{path}")
        result = self._run(self.adb_command(device.serial, "install", "-r", str(path)), timeout=1800)
        return (result.stdout or "安装完成").strip()

    def list_package_apks(self, device: Device, package_name: str) -> list[str]:
        """Return the installed APK paths reported by the source device."""
        safe_package_name = validate_package_name(package_name)
        result = self._run(
            self.adb_command(device.serial, "shell", "pm", "path", safe_package_name),
            timeout=60,
        )
        paths: list[str] = []
        for raw_line in (result.stdout or "").splitlines():
            line = raw_line.strip()
            if not line.startswith("package:"):
                continue
            remote_path = line.partition(":")[2].strip()
            if remote_path.startswith("/") and remote_path.lower().endswith(".apk"):
                if remote_path not in paths:
                    paths.append(remote_path)
        if not paths:
            raise RuntimeError(f"未找到 APK：{safe_package_name}")
        return paths

    def pull_package_apks(
        self,
        device: Device,
        package_name: str,
        staging_dir: str | Path,
        *,
        apk_paths: Sequence[str] | None = None,
    ) -> list[Path]:
        """Pull every APK split for one installed package to a local staging directory."""
        validate_package_name(package_name)
        destination = Path(staging_dir)
        destination.mkdir(parents=True, exist_ok=True)
        local_paths: list[Path] = []
        paths = list(apk_paths) if apk_paths is not None else self.list_package_apks(device, package_name)
        for remote_path in paths:
            filename = PurePosixPath(remote_path).name
            if not filename:
                raise RuntimeError(f"源设备 APK 路径无效：{remote_path}")
            local_path = destination / filename
            self._run(
                self.adb_command(device.serial, "pull", remote_path, str(local_path)),
                timeout=600,
            )
            if not local_path.is_file() or local_path.stat().st_size == 0:
                raise RuntimeError(f"APK 拉取结果无效：{local_path}")
            local_paths.append(local_path)
        return local_paths

    def install_remote_apks(self, device: Device, remote_paths: Sequence[str]) -> str:
        """Install one APK or a split APK set already pushed to a target device."""
        paths = [validate_remote_file_path(path) for path in remote_paths]
        if not paths:
            raise ValueError("至少需要一个远端 APK 文件")
        if len(paths) == 1:
            arguments = ("shell", "pm", "install", "-r", paths[0])
        else:
            arguments = ("shell", "pm", "install-multiple", "-r", *paths)
        result = self._run(self.adb_command(device.serial, *arguments), timeout=1800)
        return (result.stdout or "安装完成").strip()

    def move_apk(
        self,
        source_device: Device,
        target_device: Device,
        package_name: str,
        staging_dir: str | Path,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, object]:
        """Move an installed package through the local host into another device."""
        if source_device.serial == target_device.serial:
            raise ValueError("源设备和目标设备不能相同")
        safe_package_name = validate_package_name(package_name)

        def report(message: str) -> None:
            if progress is not None:
                progress(message)

        report(f"正在读取源设备 {source_device.serial} 的 APK 信息：{safe_package_name}")
        apk_paths = self.list_package_apks(source_device, safe_package_name)
        report(f"源设备找到 {len(apk_paths)} 个 APK 文件")
        local_paths = self.pull_package_apks(
            source_device,
            safe_package_name,
            staging_dir,
            apk_paths=apk_paths,
        )
        for local_path in local_paths:
            report(f"已复制到本地临时目录：{local_path}")

        target_remote_paths: list[str] = []
        try:
            for local_path in local_paths:
                remote_path = validate_remote_file_path(f"{APK_REMOTE_DIR}/{local_path.name}")
                target_remote_paths.append(remote_path)
                self.push_apk(target_device, local_path, remote_path)
                report(f"已推送到目标设备 {target_device.serial}：{local_path.name}")
            report(f"正在安装到目标设备 {target_device.serial}")
            install_result = self.install_remote_apks(target_device, target_remote_paths)
            report(f"目标设备安装完成：{target_device.serial}")
            return {
                "package_name": safe_package_name,
                "local_paths": [str(path) for path in local_paths],
                "remote_paths": list(target_remote_paths),
                "install_result": install_result,
            }
        finally:
            for remote_path in target_remote_paths:
                self.remove_remote_file(target_device, remote_path)
            if target_remote_paths:
                report(f"已清理目标设备临时 APK：{target_device.serial}")

    def download_apk(self, url: str, target_path: str | Path) -> Path:
        """Download one APK URL to a local staging path."""
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.downloader(normalize_download_url(url), str(target))
        if not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError(f"APK 下载结果无效：{url}")
        return target

    def list_third_party_packages(self, device: Device) -> set[str]:
        result = self._run(
            self.adb_command(device.serial, "shell", "pm", "list", "packages", "-3"),
            timeout=60,
        )
        return {
            line.strip().removeprefix("package:")
            for line in (result.stdout or "").splitlines()
            if line.strip().startswith("package:")
        }

    def push_apk(self, device: Device, local_path: str | Path, remote_path: str) -> str:
        self._run(
            self.adb_command(device.serial, "push", str(local_path), remote_path),
            timeout=600,
        )
        return remote_path

    def download_apk_to_device(self, device: Device, url: str, remote_path: str) -> str:
        """Download an APK directly on the cloud phone into its temporary directory."""
        self._run(
            self.adb_command(
                device.serial,
                "shell",
                "curl",
                "-fL",
                "-o",
                remote_path,
                normalize_download_url(url),
            ),
            timeout=1800,
        )
        return remote_path

    def install_remote_apk(self, device: Device, remote_path: str) -> str:
        result = self._run(
            self.adb_command(device.serial, "shell", "pm", "install", "-r", remote_path),
            timeout=1800,
        )
        return (result.stdout or "安装完成").strip()

    def remove_remote_file(self, device: Device, remote_path: str) -> None:
        try:
            self._run(
                self.adb_command(device.serial, "shell", "rm", "-f", remote_path),
                timeout=30,
            )
        except RuntimeError:
            pass

    def install_apk_from_url(self, device: Device, url: str) -> dict[str, str]:
        """Download, install, and identify one APK assigned to a device."""
        filename = _apk_filename(url)
        app_name = extract_apk_app_name(url)
        package_hint = extract_apk_package_name(url)
        remote_path = f"{APK_REMOTE_DIR}/{filename}"
        packages_before = self.list_third_party_packages(device)
        try:
            self.download_apk_to_device(device, url, remote_path)
            self.install_remote_apk(device, remote_path)
            packages_after = self.list_third_party_packages(device)
            if package_hint and package_hint in packages_after:
                package_name = package_hint
            else:
                newly_installed = sorted(packages_after - packages_before)
                if len(newly_installed) != 1:
                    raise RuntimeError("安装完成但无法唯一确定 APK 包名")
                package_name = newly_installed[0]
            return {
                "app_name": app_name,
                "package_name": package_name,
                "url": url,
                "remote_path": remote_path,
            }
        finally:
            self.remove_remote_file(device, remote_path)

    def run_airtest(self, device: Device, script_path: str | Path) -> None:
        path = Path(script_path)
        if not path.exists():
            raise FileNotFoundError(f"Airtest 脚本不存在：{path}")
        AIRTEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "airtest",
            "run",
            str(path),
            "--device",
            f"Android:///{device.serial}",
            "--log",
            str(AIRTEST_LOG_DIR / device.serial.replace(":", "_")),
        ]
        env = None
        if path.resolve() == AIRTEST_LOGIN_SCRIPT_PATH.resolve():
            package_name = device.package_name.strip()
            if not package_name:
                raise RuntimeError(
                    f"Device {device.serial} has no package name for the bundled Airtest flow"
                )
            env = os.environ.copy()
            env["AIRTEST_PACKAGE_NAME"] = package_name
            env["AIRTEST_APP_NAME"] = device.app_name.strip()
        self._run(command, timeout=3600, env=env)
        return None

    def run_login_autofill(self, device: Device, script_path: str | Path) -> str:
        """Fill one device's current native login form using its Android UI tree."""
        if not AUTO_LOGIN_ACCOUNT or not AUTO_LOGIN_PASSWORD:
            raise ValueError("Set YUNANDROID_LOGIN_ACCOUNT and YUNANDROID_LOGIN_PASSWORD before auto login")
        path = Path(script_path)
        if not path.is_file():
            raise FileNotFoundError(f"Airtest 登录脚本不存在：{path}")
        command = [
            self.python_executable,
            str(path),
            "--device",
            device.serial,
            "--adb-executable",
            str(self.adb_path),
            "--account",
            AUTO_LOGIN_ACCOUNT,
            "--password",
            AUTO_LOGIN_PASSWORD,
        ]
        self._run(command, timeout=3600)
        return "账号密码已填入，已点击登录"

    def clear_third_party(self, device: Device) -> str:
        listed = self._run(self.adb_command(device.serial, "shell", "pm", "list", "packages", "-3"), timeout=60)
        packages = [
            line.strip().removeprefix("package:")
            for line in (listed.stdout or "").splitlines()
            if line.strip().startswith("package:")
        ]
        if not packages:
            return "未发现第三方应用"
        failures = []
        for package in packages:
            try:
                self._run(self.adb_command(device.serial, "uninstall", package), timeout=120)
            except RuntimeError as exc:
                failures.append(f"{package}: {exc}")
        if failures:
            raise RuntimeError("部分卸载失败：" + "; ".join(failures))
        return f"已清理 {len(packages)} 个第三方应用"

    def screenshot(self, device: Device, output_path: str | Path) -> str:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        remote_path = "/sdcard/mvp_screenshot.png"
        try:
            self._run(self.adb_command(device.serial, "shell", "screencap", remote_path), timeout=60)
            self._run(self.adb_command(device.serial, "pull", remote_path, str(path)), timeout=120)
        finally:
            try:
                self._run(self.adb_command(device.serial, "shell", "rm", "-f", remote_path), timeout=30)
            except RuntimeError:
                pass
        if not path.exists() or path.stat().st_size <= 1024:
            raise RuntimeError("截图文件无效")
        return str(path)

    def start_scrcpy(self, device: Device):
        if not self.scrcpy_path.is_file():
            raise FileNotFoundError(f"scrcpy 不存在：{self.scrcpy_path}")
        device_name = device.app_name.strip() or device.id
        window_title = f"{device_name} ({device.remote_address})"
        window_geometry = self._scrcpy_window_layout.get(device.serial)
        window_arguments = [f"--window-title={window_title}"]
        if window_geometry is not None:
            x, y, width, height = window_geometry
            window_arguments.extend(
                [
                    f"--window-x={x}",
                    f"--window-y={y}",
                    f"--window-width={width}",
                    f"--window-height={height}",
                ]
            )
        env = os.environ.copy()
        env["ADB"] = str(self.adb_path.resolve())
        SCRCPY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        output = tempfile.NamedTemporaryFile(
            mode="w+b", prefix="scrcpy-", suffix=".log",
            dir=SCRCPY_LOG_DIR, delete=False,
        )
        try:
            process = self.popen(
                [str(self.scrcpy_path), "-s", device.serial,
                 "--verbosity=verbose", *window_arguments],
                cwd=str(self.scrcpy_path.parent),
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        except Exception:
            output.close()
            Path(output.name).unlink(missing_ok=True)
            raise
        output.close()
        self._scrcpy_log_paths[id(process)] = Path(output.name)
        self._scrcpy_processes[device.serial] = process
        return process

    def scrcpy_diagnostics(self, process: object) -> str:
        """Summarize process, actual window, and recent scrcpy output for the UI."""
        pid = getattr(process, "pid", None)
        poll = getattr(process, "poll", None)
        try:
            returncode = poll() if callable(poll) else "未知"
        except Exception as exc:
            returncode = f"查询失败: {exc}"
        state = "未退出" if returncode is None else f"退出码={returncode}"
        parts = [f"PID={pid if pid is not None else '未知'}", state]

        if sys.platform != "win32":
            parts.append("当前平台无法检测窗口")
        elif not isinstance(pid, int) or pid <= 0:
            parts.append("没有有效 PID，无法检测窗口")
        else:
            hwnd = self._enumerate_scrcpy_windows({pid}).get(pid)
            if hwnd is None:
                parts.append("未找到窗口")
            else:
                user32 = ctypes.windll.user32
                visible = bool(user32.IsWindowVisible(hwnd))
                rectangle = wintypes.RECT()
                bounds = "未知"
                if user32.GetWindowRect(hwnd, ctypes.byref(rectangle)):
                    bounds = (
                        f"({rectangle.left},{rectangle.top})-"
                        f"({rectangle.right},{rectangle.bottom})"
                    )
                parts.append(f"窗口 HWND={hwnd}，可见={visible}，位置={bounds}")

        log_path = self._scrcpy_log_paths.get(id(process))
        if log_path is not None:
            parts.append(f"输出文件={log_path}")
            try:
                with log_path.open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, stream.tell() - 4096))
                    output = stream.read().decode("utf-8", errors="replace")
                lines = output.splitlines()[-12:]
                if lines:
                    parts.append("最近输出=" + " | ".join(lines)[-3000:])
            except OSError as exc:
                parts.append(f"读取输出失败={exc}")
        return "；".join(parts)

    @staticmethod
    def _process_is_alive(process: object) -> bool:
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return True
        try:
            return poll() is None
        except Exception:
            return False

    def _enumerate_scrcpy_windows(self, process_ids: set[int]) -> dict[int, int]:
        """Return one top-level window handle for each tracked process ID."""
        if sys.platform != "win32" or not process_ids:
            return {}
        try:
            user32 = ctypes.windll.user32
        except (AttributeError, OSError):
            return {}

        handles: dict[int, int] = {}
        callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

        @callback_type(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _lparam):
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            pid = int(process_id.value)
            if pid in process_ids and pid not in handles:
                handles[pid] = int(hwnd)
            return True

        user32.EnumWindows(callback, 0)
        return handles

    def _set_scrcpy_window_position(
        self,
        hwnd: int,
        geometry: tuple[int, int, int, int],
    ) -> bool:
        if sys.platform != "win32":
            return False
        try:
            user32 = ctypes.windll.user32
            x, y, width, height = geometry
            flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE
            return bool(user32.SetWindowPos(hwnd, 0, x, y, width, height, flags))
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def arrange_scrcpy_windows(
        self,
        screen_geometry: tuple[int, int, int, int],
        columns: int,
        top_margin: int = 24,
        bottom_margin: int = 24,
    ) -> int:
        """Tile tracked Scrcpy windows in a top-to-bottom grid.

        The operation is intentionally limited to processes started by this
        ``DeviceOperations`` instance. It returns the number of windows moved.
        """
        if sys.platform != "win32":
            return 0

        tracked: list[tuple[str, int]] = []
        for serial, process in tuple(self._scrcpy_processes.items()):
            if not self._process_is_alive(process):
                continue
            try:
                process_id = int(getattr(process, "pid"))
            except (AttributeError, TypeError, ValueError):
                continue
            if process_id > 0:
                tracked.append((serial, process_id))

        handles = self._enumerate_scrcpy_windows({process_id for _, process_id in tracked})
        visible = [
            (serial, process_id, handles[process_id])
            for serial, process_id in tracked
            if process_id in handles
        ]
        if not visible:
            self._last_scrcpy_arrangement = {}
            return 0

        rectangles = calculate_scrcpy_window_layout(
            device_count=len(visible),
            columns=columns,
            screen_geometry=screen_geometry,
            top_margin=top_margin,
            bottom_margin=bottom_margin,
        )
        arrangement = {
            serial: rectangle
            for (serial, _process_id, _hwnd), rectangle in zip(visible, rectangles)
        }
        self._last_scrcpy_arrangement = arrangement
        self._scrcpy_window_layout.update(arrangement)

        moved = 0
        for (_serial, _process_id, hwnd), rectangle in zip(visible, rectangles):
            if self._set_scrcpy_window_position(hwnd, rectangle):
                moved += 1
        return moved

    def close_scrcpy_windows(self) -> int:
        """Terminate all Scrcpy processes started by this operation service."""
        processes = tuple(self._scrcpy_processes.values())
        self._scrcpy_processes.clear()
        closed = 0
        for process in processes:
            closed += 1
            if not self._process_is_alive(process):
                continue
            terminate = getattr(process, "terminate", None)
            if not callable(terminate):
                continue
            try:
                terminate()
            except Exception:
                continue
            wait = getattr(process, "wait", None)
            if callable(wait):
                try:
                    wait(timeout=1)
                except subprocess.TimeoutExpired:
                    kill = getattr(process, "kill", None)
                    if callable(kill):
                        try:
                            kill()
                        except Exception:
                            pass
                except Exception:
                    pass
        self._last_scrcpy_arrangement = {}
        return closed

    def cleanup_owned_processes(self) -> dict[str, int]:
        """Stop Scrcpy and interactive ADB processes owned by this service."""
        scrcpy_count = self.close_scrcpy_windows()
        shell_processes = tuple(self._adb_shell_processes.values())
        self._adb_shell_processes.clear()
        for process in shell_processes:
            self._terminate_owned_process(process)
        return {"scrcpy": scrcpy_count, "adb_shell": len(shell_processes)}

    @staticmethod
    def _terminate_owned_process(process: object | None) -> None:
        if process is None:
            return
        poll = getattr(process, "poll", None)
        if callable(poll):
            try:
                if poll() is not None:
                    return
            except Exception:
                return
        terminate = getattr(process, "terminate", None)
        if not callable(terminate):
            return
        try:
            terminate()
        except Exception:
            return
        wait = getattr(process, "wait", None)
        if not callable(wait):
            return
        try:
            wait(timeout=2)
        except Exception:
            kill = getattr(process, "kill", None)
            if callable(kill):
                try:
                    kill()
                    wait(timeout=2)
                except Exception:
                    pass

    def start_adb_shell(self, device: Device):
        """Start an interactive root ADB shell in a project-local terminal."""
        root_result = self._run(self.adb_command(device.serial, "root"), timeout=60)
        root_output = f"{root_result.stdout or ''} {root_result.stderr or ''}".lower()
        if "cannot run as root" in root_output:
            raise RuntimeError(f"Device cannot run as root: {root_output.strip()}")

        temp_dir = PROJECT_ROOT / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        popen_kwargs = {"cwd": str(temp_dir)}
        if self.popen is subprocess.Popen:
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        command = self.adb_command(device.serial, "shell")
        process = self.popen(command, **popen_kwargs)
        self._adb_shell_processes[device.serial] = process
        return process

    def start_scrcpy_batch(self, devices: Sequence[Device]) -> list[object]:
        """Launch one independent scrcpy process for every selected device."""
        return [self.start_scrcpy(device) for device in devices]

    def restart_app(self, device: Device, package_name: str) -> str:
        safe_package_name = validate_package_name(package_name)
        self._run(
            self.adb_command(
                device.serial,
                "shell",
                "am",
                "force-stop",
                safe_package_name,
            ),
            timeout=30,
        )
        started = self._run(
            self.adb_command(
                device.serial,
                "shell",
                "monkey",
                "-p",
                safe_package_name,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ),
            timeout=60,
        )
        output = f"{started.stdout or ''} {started.stderr or ''}".lower()
        if "no activities found to run" in output:
            raise RuntimeError(f"应用没有可启动的 Launcher Activity：{safe_package_name}")
        return f"已重启 {safe_package_name}"

    def pull_artifact(
        self,
        device: Device,
        artifact_type: str,
        package_name: str,
        destination: str | Path,
    ) -> Path:
        """Pull one package-scoped SF artifact after switching the selected device to root."""
        artifact = str(artifact_type).strip()
        destination_dir = Path(destination)
        destination_dir.mkdir(parents=True, exist_ok=True)
        local_path = destination_dir / artifact
        remote_path = build_artifact_remote_path(artifact, package_name)

        self._ensure_adb_root(device)
        self._run(
            self.adb_command(device.serial, "pull", remote_path, str(local_path)),
            timeout=300,
        )
        if not local_path.is_file() or local_path.stat().st_size == 0:
            raise RuntimeError(f"拉取结果无效：{local_path}")
        return local_path

    def pull_remote_file(
        self,
        device: Device,
        remote_path: str,
        destination: str | Path,
    ) -> Path:
        """Pull one validated absolute Android file into a local directory."""
        safe_remote_path = validate_remote_file_path(remote_path)
        remote_name = PurePosixPath(safe_remote_path).name
        if not remote_name:
            raise ValueError("远端目标路径必须指向文件")

        destination_dir = Path(destination)
        destination_dir.mkdir(parents=True, exist_ok=True)
        local_path = destination_dir / remote_name
        local_path.unlink(missing_ok=True)

        self._ensure_adb_root(device)
        self._run(
            self.adb_command(device.serial, "pull", safe_remote_path, str(local_path)),
            timeout=300,
        )
        if not local_path.is_file() or local_path.stat().st_size == 0:
            local_path.unlink(missing_ok=True)
            raise RuntimeError(f"拉取结果无效：{local_path}")
        return local_path

    def pull_json_files(
        self,
        device: Device,
        package_name: str,
        destination: str | Path,
    ) -> list[Path]:
        """Pull every JSON file from the package data directory as one snapshot."""
        safe_package_name = validate_package_name(package_name)
        destination_dir = Path(destination)
        destination_dir.parent.mkdir(parents=True, exist_ok=True)
        if destination_dir.exists() and not destination_dir.is_dir():
            raise RuntimeError(f"JSON 目标路径不是目录：{destination_dir}")

        remote_dir = JSON_REMOTE_DIR.format(package_name=safe_package_name)
        staging_dir = Path(
            tempfile.mkdtemp(
                # Windows ADB and tempfile both mishandle Unicode path components
                # in this workflow; the final destination can remain localized.
                prefix=".json-pull-",
                dir=str(destination_dir.parent),
            )
        )
        try:
            self._ensure_adb_root(device)
            remote_files = self._list_remote_json_files(device, remote_dir)
            if not remote_files:
                raise RuntimeError(f"远端目录没有 JSON 文件：{remote_dir}")

            staged_paths: list[Path] = []
            for remote_path, filename in remote_files:
                staged_path = staging_dir / filename
                self._run(
                    self.adb_command(device.serial, "pull", remote_path, filename),
                    timeout=300,
                    cwd=staging_dir,
                )
                if not staged_path.is_file() or staged_path.stat().st_size == 0:
                    raise RuntimeError(f"JSON 拉取结果无效：{staged_path}")
                staged_paths.append(staged_path)

            if destination_dir.exists():
                shutil.rmtree(destination_dir)
            staging_dir.replace(destination_dir)
            return [destination_dir / path.name for path in staged_paths]
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    def inspect_daily_json(
        self,
        device: Device,
        package_name: str,
        date_text: str,
    ) -> dict[str, int | bool | None]:
        """Return existence, mtime, size, and device time for one dated JSON."""
        safe_package_name = validate_package_name(package_name)
        try:
            parsed_date = datetime.strptime(str(date_text), "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("监控日期必须使用 YYYY-MM-DD 格式") from exc
        normalized_date = parsed_date.strftime("%Y-%m-%d")
        if normalized_date != str(date_text):
            raise ValueError("监控日期必须使用 YYYY-MM-DD 格式")

        remote_path = (
            f"{JSON_REMOTE_DIR.format(package_name=safe_package_name)}/"
            f"{normalized_date}.json"
        )
        probe_script = (
            f"if [ -f '{remote_path}' ]; then "
            f"meta=$(stat -c '%Y|%s' '{remote_path}' 2>/dev/null || "
            f"toybox stat -c '%Y|%s' '{remote_path}') || exit 3; "
            "now=$(date +%s) || exit 4; "
            "printf 'OK|%s|%s\\n' \"$meta\" \"$now\"; "
            "else now=$(date +%s) || exit 4; "
            "printf 'MISSING|||%s\\n' \"$now\"; fi"
        )

        self._ensure_adb_root(device)
        result = self._run(
            self.adb_command(device.serial, "shell", probe_script),
            timeout=60,
        )
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        output = lines[-1] if lines else ""
        parts = output.split("|")
        try:
            if len(parts) == 4 and parts[0] == "OK":
                return {
                    "exists": True,
                    "mtime_epoch": int(parts[1]),
                    "size": int(parts[2]),
                    "device_epoch": int(parts[3]),
                }
            if len(parts) == 4 and parts[0] == "MISSING":
                return {
                    "exists": False,
                    "mtime_epoch": None,
                    "size": None,
                    "device_epoch": int(parts[3]),
                }
        except ValueError as exc:
            raise RuntimeError(f"无法解析设备 JSON 元数据：{output!r}") from exc
        raise RuntimeError(f"无法解析设备 JSON 元数据：{output!r}")

    def _list_remote_json_files(
        self,
        device: Device,
        remote_dir: str,
    ) -> list[tuple[str, str]]:
        result = self._run(
            self.adb_command(
                device.serial,
                "shell",
                "find",
                remote_dir,
                "-maxdepth",
                "1",
                "-type",
                "f",
                "-name",
                "*.json",
                "-print",
            ),
            timeout=60,
        )
        remote_files: list[tuple[str, str]] = []
        seen_names: set[str] = set()
        prefix = remote_dir.rstrip("/") + "/"
        for line in (result.stdout or "").splitlines():
            remote_path = line.strip()
            if not remote_path.startswith(prefix):
                continue
            filename = remote_path[len(prefix) :]
            if not filename or not filename.lower().endswith(".json"):
                continue
            self._validate_remote_json_filename(filename)
            if filename in seen_names:
                continue
            seen_names.add(filename)
            remote_files.append((remote_path, filename))
        return sorted(remote_files, key=lambda item: item[1].casefold())

    @staticmethod
    def _validate_remote_json_filename(filename: str) -> None:
        """Reject remote names that could escape or be invalid on Windows."""
        if (
            filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
            or any(ord(char) < 32 for char in filename)
            or any(char in '<>:"|?*' for char in filename)
            or filename.endswith((".", " "))
        ):
            raise RuntimeError(f"远端 JSON 文件名无效：{filename!r}")
        stem = filename.split(".", 1)[0].rstrip(". ").upper()
        if stem in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{index}" for index in range(1, 10)),
            *(f"LPT{index}" for index in range(1, 10)),
        }:
            raise RuntimeError(f"远端 JSON 文件名无效：{filename!r}")

    def push_lua(
        self,
        device: Device,
        lua_path: str | Path,
        package_name: str,
        destination: str = LUA_DESTINATION_DEFAULT,
    ) -> str:
        source = Path(lua_path)
        if not source.is_file():
            raise FileNotFoundError(f"Lua 文件不存在：{source}")
        safe_target = build_lua_target(package_name, destination)
        self._ensure_adb_root(device)
        self._run(self.adb_command(device.serial, "push", str(source), safe_target), timeout=300)
        return f"已推送到 {safe_target}"

    def clear_sf_json(self, device: Device, package_name: str) -> str:
        safe_package_name = validate_package_name(package_name)
        remote_dir = JSON_REMOTE_DIR.format(package_name=safe_package_name)
        self._ensure_adb_root(device)
        for remote_path, filename in self._list_remote_json_files(device, remote_dir):
            if not _JSON_DATE_FILENAME_PATTERN.fullmatch(filename):
                continue
            self._run(
                self.adb_command(device.serial, "shell", "rm", "-f", remote_path),
                timeout=60,
            )
        return remote_dir

    def push_file(self, device: Device, local_path: str | Path, remote_path: str) -> str:
        source = Path(local_path)
        if not source.is_file():
            raise FileNotFoundError(f"本地文件不存在：{source}")
        safe_remote_path = validate_remote_file_path(remote_path)
        self._ensure_adb_root(device)
        self._run(
            self.adb_command(device.serial, "push", str(source), safe_remote_path),
            timeout=300,
        )
        return safe_remote_path

"""Start an Android package and open its account/password login form.

The module is intentionally usable in two ways:

* run it directly with its package-name CLI;
* import ``run_flow`` from the desktop application's task worker (or execute
  it through Airtest with the documented environment variables).

The native startup screens use the Android control tree. Cocos2d-rendered
update screens use fixed taps, with Airtest template fallback when needed.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from xml.etree import ElementTree

import numpy as np

try:
    import pandas as pd
except ImportError:  # pragma: no cover - only used when workbook lookup is requested
    pd = None

try:
    from airtest.core.api import (
        Template,
        auto_setup,
        device,
        double_click,
        exists,
        sleep,
        start_app,
        text,
        touch,
    )
except ImportError:  # pragma: no cover - allows pure helper imports without Airtest
    Template = None  # type: ignore[assignment]

    def _missing_airtest(*_: Any, **__: Any) -> Any:
        raise RuntimeError("Airtest is required to execute the device flow")

    auto_setup = device = double_click = exists = sleep = start_app = text = touch = _missing_airtest


ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "templates"
DEFAULT_ACCOUNT_WORKBOOK = ROOT / "accounts.xlsx"
DEFAULT_ACCOUNT = os.environ.get("YUNANDROID_LOGIN_ACCOUNT", "")
DEFAULT_PASSWORD = os.environ.get("YUNANDROID_LOGIN_PASSWORD", "")
DEFAULT_POLL_INTERVAL = 0.5

logger = logging.getLogger("airtest.login")

SCREEN_CENTER_1280 = (640, 360)
FIRST_UPDATE_CONFIRM = (748, 451)
FINAL_UPDATE_CONFIRM = (640, 428)
ANNOUNCEMENT_CLOSE = (1088, 64)
UPDATE_DIALOG_REGION = (470, 260, 810, 390)
UI_DUMP_PATH = "/sdcard/airtest_startup_ui.xml"
_BOUNDS = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")


class FlowError(RuntimeError):
    """Raised when a required screen in the startup flow cannot be completed."""


# Metadata is copied from the active Template calls in reference/python/AirTest1.py.
# Templates without metadata use Airtest's native screen-size matching behavior.
TEMPLATE_SPECS: dict[str, dict[str, Any]] = {
    "tp-20250213-153656.png": {},
    "tp-20250213-153801.png": {},
    "tp-20250213-153827.png": {},
    "tpl1739430342431.png": {"record_pos": (-0.131, 0.237), "resolution": (1280, 720)},
    "tpl1739432016210.png": {},
    "tpl1739432042928.png": {},
    "tpl1739434367838.png": {"record_pos": (0.002, 0.005), "resolution": (1280, 720)},
    "tpl1739435975306.png": {"record_pos": (0.127, 0.075), "resolution": (1280, 720)},
    "tpl1739439929552.png": {"record_pos": (0.128, 0.056), "resolution": (1280, 720)},
    "tpl1739440885832.png": {"record_pos": (-0.223, 0.24), "resolution": (1280, 720)},
    "tpl1739440903951.png": {"record_pos": (-0.002, 0.159), "resolution": (1280, 720)},
    "tpl1740018915193.png": {"record_pos": (-0.034, -0.038), "resolution": (3040, 1440)},
    "tpl1740032435096.png": {"record_pos": (-0.003, 0.011), "resolution": (3040, 1440)},
    "tpl1740032551637.png": {"record_pos": (0.001, -0.007), "resolution": (3040, 1440)},
    "tpl1740033329454.png": {"record_pos": (0.298, -0.194), "resolution": (3040, 1440)},
    "tpl1740034373314.png": {"record_pos": (-0.228, 0.186), "resolution": (3040, 1440)},
    "tpl1740043022209.png": {"record_pos": (-0.011, 0.034), "resolution": (3040, 1440)},
    "tpl1740043423429.png": {"record_pos": (-0.013, 0.032), "resolution": (3040, 1440)},
    "tpl1740053457788.png": {"record_pos": (-0.0, 0.065), "resolution": (3040, 1440)},
    "tpl1740055990877.png": {"record_pos": (0.293, -0.192), "resolution": (3040, 1440)},
    "tpl1740056009834.png": {"record_pos": (0.297, -0.19), "resolution": (3040, 1440)},
    "tpl1740102723199.png": {"record_pos": (-0.033, -0.074), "resolution": (3040, 1440)},
    "tpl1740124090169.png": {"record_pos": (-0.06, 0.083), "resolution": (1280, 720)},
    "tpl1740297017400.png": {"record_pos": (0.01, 0.011), "resolution": (1920, 1080)},
    "tpl1740302000354.png": {"record_pos": (-0.002, 0.014), "resolution": (1280, 720)},
    "tpl1740452568444.png": {"record_pos": (0.095, 0.102), "resolution": (1280, 720)},
    "tpl1740452580253.png": {"record_pos": (0.105, 0.1), "resolution": (1280, 720)},
    "tpl1740452598056.png": {"record_pos": (0.015, -0.072), "resolution": (1280, 720)},
    "tpl1740452610862.png": {"record_pos": (0.032, -0.006), "resolution": (1280, 720)},
    "tpl1740489618650.png": {"record_pos": (0.131, 0.021), "resolution": (1600, 900)},
    "tpl1740489982251.png": {"record_pos": (0.161, 0.024), "resolution": (1600, 900)},
    "tpl1740554932789.png": {"record_pos": (0.232, -0.005), "resolution": (1920, 1080)},
    "tpl1741067375534.png": {"record_pos": (0.01, 0.078), "resolution": (1920, 1080)},
    "tpl1743671008745.png": {"record_pos": (-0.053, 0.068), "resolution": (1920, 1080)},
    "tpl1786946511109.png": {"record_pos": (0.083, 0.033), "resolution": (2560, 1440)},
    "tpl1786946597559.png": {"record_pos": (0.057, -0.07), "resolution": (2560, 1440)},
    "tpl1786947384404.png": {"record_pos": (0.045, -0.018), "resolution": (2560, 1440)},
    "tpl1787822696272.png": {"record_pos": (0.131, 0.021), "resolution": (1280, 720)},
    "tpl1787822730481.png": {"record_pos": (0.127, 0.075), "resolution": (1280, 720)},
    "tpl1787823743488.png": {"record_pos": (0.013, -0.071), "resolution": (1280, 720)},
}

LOGIN_MARKERS = (
    "tpl1787822730481.png",
    "tpl1786946511109.png",
    "tpl1740452568444.png",
    "tpl1739435975306.png",
)


def template_path(filename: str) -> Path:
    """Return a validated absolute path for a bundled template image."""
    if filename not in TEMPLATE_SPECS:
        raise ValueError(f"Unknown Airtest template: {filename}")
    path = TEMPLATE_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"Airtest template is missing: {path}")
    return path


def _template(filename: str) -> Any:
    if Template is None:  # pragma: no cover - covered by the import fallback above
        raise RuntimeError("Airtest is required to create image templates")
    return Template(str(template_path(filename)), **TEMPLATE_SPECS[filename])


def _wait_for(filename: str, timeout: float) -> bool:
    """Poll for one image without allowing a missing optional prompt to hang."""
    deadline = time.monotonic() + max(0.0, timeout)
    image = _template(filename)
    while True:
        try:
            if exists(image):
                return True
        except Exception as exc:  # Airtest can fail while a device reconnects.
            logger.debug("Template probe failed for %s: %s", filename, exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        sleep(min(DEFAULT_POLL_INTERVAL, remaining))


def _wait_for_any(filenames: tuple[str, ...], timeout: float) -> str | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for filename in filenames:
            if _wait_for(filename, 0):
                return filename
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sleep(min(DEFAULT_POLL_INTERVAL, remaining))


def _click_if_present(filename: str, timeout: float = 0.0) -> bool:
    if not _wait_for(filename, timeout):
        return False
    touch(_template(filename))
    return True


def _click_any(filenames: tuple[str, ...], timeout: float = 0.0) -> str | None:
    filename = _wait_for_any(filenames, timeout)
    if filename is None:
        return None
    touch(_template(filename))
    return filename


def read_ui_hierarchy() -> str:
    adb = device().adb
    for attempt in range(3):
        try:
            result = adb.shell(f"uiautomator dump {UI_DUMP_PATH}")
            if "dumped to" not in result:
                raise FlowError(f"Android control dump did not complete: {result}")
            xml = adb.shell(f"cat {UI_DUMP_PATH}")
            _ui_nodes(xml)
            return xml
        except Exception as exc:
            if attempt == 2:
                raise FlowError(f"Android control tree unavailable: {exc}") from exc
            logger.warning("Android control dump retry %d: %s", attempt + 1, exc)
            sleep(0.5)
        finally:
            try:
                adb.shell(f"rm -f {UI_DUMP_PATH}")
            except Exception:
                logger.debug("Could not remove temporary Android control dump")
    raise AssertionError("unreachable")


def _ui_nodes(xml: str):
    try:
        return [node for node in ElementTree.fromstring(xml).iter("node")
                if node.get("enabled") != "false"]
    except ElementTree.ParseError as exc:
        raise FlowError(f"invalid Android control tree: {exc}") from exc


def _control_center(nodes, label: str):
    if not nodes:
        return None
    if len(nodes) != 1:
        raise FlowError(f"{label} control is ambiguous ({len(nodes)} matches)")
    match = _BOUNDS.fullmatch(nodes[0].get("bounds", ""))
    if match is None:
        raise FlowError(f"{label} control has invalid bounds")
    x1, y1, x2, y2 = map(int, match.groups())
    if x2 <= x1 or y2 <= y1:
        raise FlowError(f"{label} control has empty bounds")
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def find_privacy_accept(xml: str, package_name: str):
    nodes = _ui_nodes(xml)
    title = any(node.get("package") == package_name
                and node.get("resource-id", "").endswith(":id/tv_html")
                and node.get("text", "").startswith("用户协议与隐私政策提示")
                for node in nodes)
    if not title:
        return None
    return _control_center([
        node for node in nodes if node.get("package") == package_name
        and node.get("resource-id", "").endswith(":id/btn_positive")
        and node.get("text") == "同意并进入"
    ], "privacy accept")


def find_permission_allow(xml: str):
    return _control_center([
        node for node in _ui_nodes(xml)
        if node.get("package") in {"com.android.packageinstaller", "com.android.permissioncontroller"}
        and node.get("resource-id", "").endswith(":id/permission_allow_button")
        and node.get("text") == "允许"
    ], "permission allow")


def find_account_login(xml: str, package_name: str):
    return _control_center([
        node for node in _ui_nodes(xml) if node.get("package") == package_name
        and node.get("resource-id", "").endswith(":id/account_login")
        and node.get("text") == "账密登录"
    ], "account login")


def has_account_password_form(xml: str, package_name: str) -> bool:
    fields = [
        node for node in _ui_nodes(xml)
        if node.get("package") == package_name
        and node.get("class") == "android.widget.EditText"
        and node.get("resource-id", "").endswith(":id/et_content")
    ]
    return len(fields) == 2 and {node.get("password") for node in fields} == {"false", "true"}


def _wait_for_control(finder, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        position = finder(read_ui_hierarchy())
        if position is not None:
            return position
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sleep(min(DEFAULT_POLL_INTERVAL, remaining))


def _dialog_changed(before: np.ndarray, after: np.ndarray, region) -> bool:
    if before.shape != after.shape or before.ndim != 3:
        raise FlowError("cannot compare update confirmation screenshots")
    x1, y1, x2, y2 = region
    old = before[y1:y2, x1:x2].astype(np.int16)
    new = after[y1:y2, x1:x2].astype(np.int16)
    if old.size == 0 or new.size == 0:
        raise FlowError("update confirmation region is outside the screen")
    return float(np.abs(old - new).mean()) > 12


def _raw_update_screenshot() -> np.ndarray:
    screen = device().snapshot(filename=None)
    if not isinstance(screen, np.ndarray) or screen.ndim != 3 or screen.shape[2] < 3:
        raise FlowError(f"raw device screenshot unavailable (got {type(screen).__name__})")
    return screen


def _tap_update_confirm(position, fallback_template: str, region):
    before = _raw_update_screenshot()
    if before.shape[:2] != (720, 1280):
        raise FlowError("fixed update coordinates require a 1280x720 landscape screen")
    touch(position)
    clicked_at = time.monotonic()
    sleep(0.6)
    if _dialog_changed(before, _raw_update_screenshot(), region):
        logger.info("Update confirmation responded at fixed position %s", position)
        return clicked_at
    logger.warning("Update confirmation did not respond at %s; trying Airtest template", position)
    if _click_if_present(fallback_template, 3.0):
        return time.monotonic()
    raise FlowError(f"update confirmation did not respond at {position}")


def setup_device(device_uri: str | None) -> None:
    """Configure Airtest for direct Python execution when a URI is supplied."""
    if device_uri:
        auto_setup(__file__, devices=[device_uri], logdir=str(ROOT / "logs"))


def load_credentials(
    app_name: str,
    package_name: str,
    workbook: str | Path | None = None,
    account: str | None = None,
    password: str | None = None,
) -> tuple[str, str]:
    """Resolve credentials, preferring explicit values over the reference workbook."""
    if (account is None) != (password is None):
        raise ValueError("account and password must be provided together")
    if account is not None and password is not None:
        return str(account).strip(), str(password).strip()

    workbook_path = Path(workbook) if workbook else DEFAULT_ACCOUNT_WORKBOOK
    if pd is None:
        raise RuntimeError("pandas is required to read the account workbook")
    if not workbook_path.is_file():
        if not DEFAULT_ACCOUNT or not DEFAULT_PASSWORD:
            raise FileNotFoundError(f"Account workbook not found and login credentials are unset: {workbook_path}")
        logger.warning("Account workbook not found at %s; using environment credentials", workbook_path)
        return DEFAULT_ACCOUNT, DEFAULT_PASSWORD

    frame = pd.read_excel(workbook_path)
    if frame.shape[1] < 4:
        raise ValueError("Account workbook must contain app, package, account, and password columns")

    # The checked-in reference workbook has mojibake column labels, so matching
    # the first four columns is deliberately the compatibility fallback.
    columns = list(frame.columns)
    aliases = (
        ("app名称", "appName", "app name", "app����"),
        ("包名", "packageName", "package name", "����"),
        ("账号", "account", "username", "�˺�"),
        ("密码", "password", "����"),
    )
    selected: list[Any] = []
    for index, candidates in enumerate(aliases):
        normalized = {str(column).strip().casefold(): column for column in columns}
        selected.append(next((normalized[c.casefold()] for c in candidates if c.casefold() in normalized), columns[index]))
    app_column, package_column, account_column, password_column = selected

    app_matches = frame[frame[app_column].astype(str).str.strip() == str(app_name).strip()]
    if app_matches.empty:
        return DEFAULT_ACCOUNT, DEFAULT_PASSWORD
    package_matches = app_matches[app_matches[package_column].astype(str).str.strip() == str(package_name).strip()]
    row = (package_matches if not package_matches.empty else app_matches).iloc[0]
    return str(row[account_column]).strip(), str(row[password_column]).strip()


def handle_privacy(timeout: float = 15.0, package_name: str | None = None) -> bool:
    """Accept the native first-run privacy dialog if it appears."""
    package = package_name or os.environ.get("AIRTEST_PACKAGE_NAME", "")
    position = _wait_for_control(lambda xml: find_privacy_accept(xml, package), timeout)
    if position is None:
        return False
    touch(position)
    logger.info("Privacy agreement accepted through Android control tree")
    sleep(1)
    return True


def handle_permissions(timeout: float = 10.0) -> bool:
    """Allow any native Android permission prompts shown during startup."""
    clicked = False
    position = _wait_for_control(find_permission_allow, timeout)
    while position is not None:
        touch(position)
        logger.info("Android permission allowed through control tree")
        clicked = True
        sleep(1)
        position = _wait_for_control(find_permission_allow, 1.5)
    if clicked:
        sleep(4)
    return clicked


def handle_update(wait_seconds: float = 300.0, timeout: float = 10.0, package_name: str | None = None) -> bool:
    """Tap both update confirmations at fixed positions, with template fallback."""
    if package_name:
        xml = read_ui_hierarchy()
        if has_account_password_form(xml, package_name) or find_account_login(xml, package_name):
            logger.info("Login page is already visible; skipping resource update")
            return True
    _tap_update_confirm(FIRST_UPDATE_CONFIRM, "tpl1739432042928.png", UPDATE_DIALOG_REGION)
    complete = _wait_for_any(("tpl1739434367838.png", "tpl1740032435096.png"), wait_seconds)
    if complete is None:
        raise FlowError("resource update completion did not appear")
    confirmed_at = _tap_update_confirm(FINAL_UPDATE_CONFIRM, "tpl1739432042928.png", UPDATE_DIALOG_REGION)
    for delay in (1.0, 2.0):
        sleep(max(0.0, confirmed_at + delay - time.monotonic()))
        double_click(SCREEN_CENTER_1280)
        logger.info("Opening animation double-click at approximately %.0fs", delay)
    return True


def open_account_login(package_name: str, timeout: float = 10.0) -> bool:
    """Close the rendered announcement, then choose the native account-login control."""
    if has_account_password_form(read_ui_hierarchy(), package_name):
        logger.info("Account/password form is already open")
        return True
    finder = lambda xml: find_account_login(xml, package_name)
    position = _wait_for_control(finder, 2.0)
    for _ in range(3):
        if position is not None:
            break
        sleep(3)
        touch(ANNOUNCEMENT_CLOSE)
        logger.info("Tapped announcement close at %s", ANNOUNCEMENT_CLOSE)
        position = _wait_for_control(finder, timeout)
    if position is None:
        raise FlowError("account-login control did not appear after closing announcement")
    touch(position)
    if not _wait_for_control(lambda xml: True if has_account_password_form(xml, package_name) else None, timeout):
        raise FlowError("account/password form did not appear after tapping account login")
    logger.info("Account-login control tapped through Android control tree")
    return True


def skip_opening_animation(timeout: float = 8.0) -> bool:
    """Click the reference opening-animation/enter-game button when present."""
    return _click_if_present("tpl1739440903951.png", timeout)


def _login_1280(account: str, password: str, timeout: float, marker: str) -> None:
    touch(_template(marker))
    if not _click_if_present("tpl1787823743488.png", timeout):
        raise FlowError("1280 login form account field was not found")
    text(account)
    # The reference form advances from account to password through the same
    # focused input; an empty text call clears any stale IME composition.
    text("")
    text(password)
    _click_if_present("tpl1739439929552.png", timeout)


def _login_2560(account: str, password: str, timeout: float) -> None:
    if not _click_if_present("tpl1786946597559.png", timeout):
        raise FlowError("2560 login form account field was not found")
    text(account)
    if not _click_if_present("tpl1786947384404.png", timeout):
        raise FlowError("2560 login form password field was not found")
    text(password)
    _click_if_present("tpl1786946511109.png", timeout)


def _login_register_form(account: str, password: str, timeout: float) -> None:
    _click_if_present("tpl1740452580253.png", timeout)
    if not _click_if_present("tpl1740452598056.png", timeout):
        raise FlowError("register/login form account field was not found")
    text(account)
    if not _click_if_present("tpl1740452610862.png", timeout):
        raise FlowError("register/login form password field was not found")
    text(password)
    _click_if_present("tpl1739439929552.png", timeout)


def login_with_credentials(
    package_name: str,
    app_name: str,
    account: str,
    password: str,
    timeout: float = 15.0,
    workbook: str | Path | None = None,
) -> bool:
    """Identify a supported login screen, fill credentials, and submit it."""
    del package_name, app_name, workbook  # kept in the signature for old callers
    marker = _wait_for_any(LOGIN_MARKERS, timeout)
    if marker is None:
        return False
    if marker in {"tpl1787822730481.png", "tpl1739435975306.png"}:
        _login_1280(account, password, timeout, marker)
    elif marker == "tpl1786946511109.png":
        _login_2560(account, password, timeout)
    else:
        _login_register_form(account, password, timeout)

    error = _wait_for_any(("tpl1740032551637.png", "tpl1740302000354.png", "tpl1740297017400.png"), 3.0)
    if error:
        return False
    # Login submission is considered successful once no account error is shown;
    # the following opening-animation click remains optional for already-entered sessions.
    return True


def run_flow(
    package_name: str,
    app_name: str = "",
    *,
    device_uri: str | None = None,
    workbook: str | Path | None = None,
    account: str | None = None,
    password: str | None = None,
    update_wait: float = 300.0,
    actions: Mapping[str, Callable[[], Any]] | None = None,
) -> bool:
    """Run startup through the account/password form without entering credentials."""
    if not str(package_name).strip():
        raise ValueError("package name is required")
    setup_device(device_uri)
    del app_name, workbook, account, password
    default_actions: dict[str, Callable[[], Any]] = {
        "privacy": lambda: handle_privacy(package_name=package_name),
        "permissions": handle_permissions,
        "update": lambda: handle_update(update_wait, package_name=package_name),
        "login": lambda: open_account_login(package_name),
    }
    selected_actions = {**default_actions, **(dict(actions) if actions else {})}

    start_app(str(package_name).strip())
    for name in ("privacy", "permissions", "update", "login"):
        outcome = selected_actions[name]()
        if name in {"update", "login"} and outcome is False:
            raise FlowError(f"{name} step did not complete")
    return True


# Compatibility names used by reference/python/AirTest1.py.
def apkPrivacy() -> bool:
    return handle_privacy()


def apkPermission() -> bool:
    return handle_permissions()


def apkLibUpdate(packageName: str = "", appName: str = "", apk_Time: float = 30.0) -> bool:
    del packageName, appName
    return handle_update(float(apk_Time))


def wdsfLogin(packageName: str, appName: str) -> bool:
    account, password = load_credentials(appName, packageName)
    return login_with_credentials(packageName, appName, account, password)


def apk_version(
    packageName: str,
    appName: str,
    serialno: str | None = None,
    apk_Time: float = 30.0,
    version_name: str | None = None,
    local_save_path: str | Path | None = None,
) -> bool:
    del version_name, local_save_path
    uri = f"Android:///{serialno}" if serialno else None
    return run_flow(packageName, appName, device_uri=uri, update_wait=float(apk_Time))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_name", help="Android package name, for example com.example.game")
    parser.add_argument("--device", dest="device_uri", help="Airtest device URI, for example Android:///127.0.0.1:5555")
    parser.add_argument("--app-name", default="", help="Application name used for workbook lookup")
    parser.add_argument("--account-workbook", type=Path, help="Workbook containing app/package/account/password columns")
    parser.add_argument("--account", help="Explicit account; must be paired with --password")
    parser.add_argument("--password", help="Explicit password; must be paired with --account")
    parser.add_argument("--update-wait", type=float, default=300.0, help="Seconds to wait for resource update")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_flow(
            args.package_name,
            args.app_name,
            device_uri=args.device_uri,
            workbook=args.account_workbook,
            account=args.account,
            password=args.password,
            update_wait=args.update_wait,
        )
    except (FlowError, OSError, RuntimeError, ValueError) as exc:
        logger.error("Airtest flow failed: %s", exc)
        return 1
    return 0


def _airtest_runner_active() -> bool:
    """Airtest executes scripts as ``__main__`` inside its unittest runner."""
    runner = sys.modules.get("airtest.cli.runner")
    return runner is not None and getattr(runner, "args", None) is not None


def _run_from_airtest_runner() -> None:
    package_name = os.environ.get("AIRTEST_PACKAGE_NAME", "").strip()
    if not package_name:
        logger.warning("AIRTEST_PACKAGE_NAME is not set; no package was started")
        return
    run_flow(
        package_name,
        os.environ.get("AIRTEST_APP_NAME", ""),
        workbook=os.environ.get("AIRTEST_ACCOUNT_WORKBOOK") or None,
        account=os.environ.get("AIRTEST_ACCOUNT"),
        password=os.environ.get("AIRTEST_PASSWORD"),
        update_wait=float(os.environ.get("AIRTEST_UPDATE_WAIT", "300")),
    )


if __name__ == "__main__" and _airtest_runner_active():  # pragma: no cover - device entry point
    _run_from_airtest_runner()
elif __name__ == "__main__":  # pragma: no cover - direct CLI entry point
    raise SystemExit(main())

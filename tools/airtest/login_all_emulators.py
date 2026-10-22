import argparse
from dataclasses import dataclass
import logging
import os
import re
import sys
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree

import numpy as np
from airtest.core.api import connect_device
from airtest.core.settings import Settings as ST


# adb executable. The UI passes its bundled adb.exe explicitly.
ADB_EXE = "adb"
DEFAULT_ACCOUNT = os.environ.get("YUNANDROID_LOGIN_ACCOUNT", "")
DEFAULT_PASSWORD = os.environ.get("YUNANDROID_LOGIN_PASSWORD", "")
AFTER_INPUT_DELAY = 0.2
UI_DUMP_PATH = "/sdcard/airtest_autofill_ui.xml"
CONSENT_POLL_ATTEMPTS = 8
CONSENT_POLL_INTERVAL = 0.5
_BOUNDS_PATTERN = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")

# Airtest keeps the current device in a process-global variable while connecting.
CONNECT_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()


def log(message):
    with PRINT_LOCK:
        print(message)


@dataclass(frozen=True)
class UiControl:
    bounds: tuple[int, int, int, int]
    checked: bool | None = None

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)


@dataclass(frozen=True)
class LoginControls:
    account: UiControl
    password: UiControl
    submit: UiControl
    agreement: UiControl | None


def _control_from_node(node: ElementTree.Element) -> UiControl:
    match = _BOUNDS_PATTERN.fullmatch(node.get("bounds", ""))
    if match is None:
        raise RuntimeError("login control has invalid bounds")
    bounds = tuple(int(value) for value in match.groups())
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        raise RuntimeError("login control has empty bounds")
    checked = None
    if node.get("checkable") == "true":
        checked = node.get("checked") == "true"
    return UiControl(bounds, checked)


def _one(nodes: list[ElementTree.Element], label: str) -> ElementTree.Element:
    if len(nodes) != 1:
        raise RuntimeError(f"{label} not found or ambiguous ({len(nodes)} matches)")
    return nodes[0]


def parse_login_controls(xml: bytes | str) -> LoginControls:
    """Find the current native login controls without relying on displayed text."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise RuntimeError(f"invalid Android UI hierarchy: {exc}") from exc
    nodes = [
        node for node in root.iter("node")
        if node.get("enabled") != "false"
    ]
    account_node = _one([
        node for node in nodes
        if node.get("class") == "android.widget.EditText"
        and node.get("resource-id", "").endswith(":id/et_content")
        and node.get("password") == "false"
    ], "account field")
    package = account_node.get("package", "")
    password_node = _one([
        node for node in nodes
        if node.get("class") == "android.widget.EditText"
        and node.get("resource-id", "").endswith(":id/et_content")
        and node.get("package") == package
        and node.get("password") == "true"
    ], "password field")
    submit_node = _one([
        node for node in nodes
        if node.get("class") == "android.widget.Button"
        and (
            node.get("resource-id", "").endswith(":id/lt_common_submit")
            or (
                node.get("resource-id", "").endswith(":id/btn_login")
                and node.get("text") == "登录"
            )
        )
        and node.get("package") == package
    ], "login button")
    agreements = [
        node for node in nodes
        if node.get("resource-id", "").endswith(":id/agreement_btn")
        and node.get("package") == package
    ]
    if len(agreements) > 1:
        raise RuntimeError("agreement control is ambiguous")
    return LoginControls(
        account=_control_from_node(account_node),
        password=_control_from_node(password_node),
        submit=_control_from_node(submit_node),
        agreement=_control_from_node(agreements[0]) if agreements else None,
    )


def read_ui_hierarchy(serial: str, adb_exe: str | None = None) -> bytes:
    executable = str(adb_exe or ADB_EXE)
    command = [executable, "-s", str(serial)]
    for attempt in range(3):
        try:
            dumped = subprocess.run(
                [*command, "shell", "uiautomator", "dump", UI_DUMP_PATH],
                capture_output=True, timeout=20,
            )
            if dumped.returncode != 0:
                raise RuntimeError(
                    "Android UI dump failed: "
                    + dumped.stderr.decode("utf-8", errors="replace").strip()
                )
            result = subprocess.run(
                [*command, "shell", "cat", UI_DUMP_PATH],
                capture_output=True, timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                raise RuntimeError("Android UI dump could not be read")
            return result.stdout
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            if attempt == 2:
                raise RuntimeError(f"Android UI dump failed for {serial}: {exc}") from exc
            log(f"[{serial}] Android UI dump retry {attempt + 1}/2: {exc}")
            time.sleep(0.5)
        finally:
            subprocess.run(
                [*command, "shell", "rm", "-f", UI_DUMP_PATH],
                capture_output=True, timeout=10,
            )
    raise AssertionError("unreachable")


def read_login_controls(serial: str, adb_exe: str | None = None) -> LoginControls:
    for attempt in range(4):
        try:
            return parse_login_controls(read_ui_hierarchy(serial, adb_exe))
        except RuntimeError as exc:
            if attempt == 3:
                raise
            log(f"[{serial}] login controls unavailable, retry {attempt + 1}/3: {exc}")
            time.sleep(0.5)
    raise AssertionError("unreachable")


def parse_consent_button(xml: bytes | str) -> UiControl | None:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise RuntimeError(f"invalid Android UI hierarchy: {exc}") from exc
    nodes = [node for node in root.iter("node") if node.get("enabled") != "false"]
    titles = [
        node for node in nodes
        if node.get("resource-id", "").endswith(":id/tv_html")
        and node.get("text", "").startswith("雷霆游戏使用许可及用户协议")
    ]
    if not titles:
        return None
    title = _one(titles, "agreement page title")
    buttons = [
        node for node in nodes
        if node.get("class") == "android.widget.Button"
        and node.get("resource-id", "").endswith(":id/btn_positive")
        and node.get("text") == "同意"
        and node.get("package") == title.get("package")
    ]
    return _control_from_node(_one(buttons, "agreement accept button"))


def accept_followup_agreement(dev, serial, adb_exe=None):
    for attempt in range(CONSENT_POLL_ATTEMPTS):
        try:
            xml = read_ui_hierarchy(serial, adb_exe)
        except RuntimeError as exc:
            if not str(exc).startswith("Android UI dump failed"):
                raise
            log(f"[{serial}] agreement page control tree unavailable: {exc}")
            xml = None
        button = parse_consent_button(xml) if xml is not None else None
        if button is not None:
            touch_position(dev, button.center)
            log(f"[{serial}] agreement page accept button tapped")
            return True
        if attempt < CONSENT_POLL_ATTEMPTS - 1:
            time.sleep(CONSENT_POLL_INTERVAL)
    log(f"[{serial}] agreement page not shown")
    return False


def agreement_is_selected(screen: np.ndarray, control: UiControl) -> bool:
    if control.checked is not None:
        return control.checked
    if screen is None or screen.ndim != 3 or screen.shape[2] < 3:
        raise RuntimeError("agreement state could not be inspected")
    x1, y1, x2, y2 = control.bounds
    center_x, center_y = control.center
    half_width = max(1, (x2 - x1) // 5)
    half_height = max(1, (y2 - y1) // 5)
    area = screen[
        center_y - half_height:center_y + half_height,
        center_x - half_width:center_x + half_width,
    ]
    if area.size == 0:
        raise RuntimeError("agreement icon is outside the screenshot")
    blue, green, red = (area[:, :, index].astype(np.int16) for index in range(3))
    purple = (blue > 150) & (blue > red * 1.3) & (red > 40) & (red < 180) & (green < 150)
    fraction = float(purple.mean())
    if fraction >= 0.6:
        return True
    if fraction <= 0.2:
        return False
    raise RuntimeError(f"agreement state is unclear (purple fraction {fraction:.2f})")


def touch_position(dev, position):
    dev.touch(position)


def input_text(dev, value, label=None):
    try:
        dev.text(value, enter=False)
        time.sleep(AFTER_INPUT_DELAY)
    except Exception as exc:
        suffix = f" [{label}]" if label else ""
        raise RuntimeError(f"input failed{suffix}: {exc}") from exc


def autofill_current_login(dev, account, password, serial, adb_exe=None):
    """Fill a native Android login form from its current accessibility tree."""
    controls = read_login_controls(serial, adb_exe)
    if controls.agreement is not None:
        screen = dev.snapshot(filename=None, quality=ST.SNAPSHOT_QUALITY)
        if not agreement_is_selected(screen, controls.agreement):
            touch_position(dev, controls.agreement.center)
            time.sleep(AFTER_INPUT_DELAY)
            screen = dev.snapshot(filename=None, quality=ST.SNAPSHOT_QUALITY)
            if not agreement_is_selected(screen, controls.agreement):
                raise RuntimeError("agreement checkbox did not become selected")
            log(f"[{serial}] agreement selected")
        else:
            log(f"[{serial}] agreement already selected")

    controls = read_login_controls(serial, adb_exe)
    touch_position(dev, controls.account.center)
    input_text(dev, account, "account")
    log(f"[{serial}] account filled")

    controls = read_login_controls(serial, adb_exe)
    touch_position(dev, controls.password.center)
    input_text(dev, password, "password")
    log(f"[{serial}] password filled")

    if dev.is_keyboard_shown():
        dev.keyevent("BACK")
    controls = read_login_controls(serial, adb_exe)
    touch_position(dev, controls.submit.center)
    log(f"[{serial}] login button tapped")
    return True


def check_adb_device(serial, adb_exe=None):
    """Verify the serial is online before Airtest initializes its capture stack."""
    executable = str(adb_exe or ADB_EXE)
    command = [executable, "-s", str(serial), "get-state"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"ADB executable unavailable: {executable}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"[{serial}] ADB device check timed out") from exc

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    state = stdout.casefold()
    if result.returncode != 0 or state != "device":
        reason = stderr or stdout or f"state={state or 'unknown'}"
        raise RuntimeError(f"[{serial}] ADB device unavailable: {reason}")
    return True


def build_device_uri(serial, adb_exe=None):
    """Build an Airtest URI, optionally forcing the project's adb executable."""
    uri = f"Android://127.0.0.1:5037/{serial}"
    if adb_exe:
        uri += f"?adb_path={quote(str(adb_exe), safe='')}"
    return uri


def process_device(serial, account, password, adb_exe=None):
    check_adb_device(serial, adb_exe)
    log(f"[{serial}] connect")
    with CONNECT_LOCK:
        dev = connect_device(build_device_uri(serial, adb_exe))
    autofill_current_login(dev, account, password, serial, adb_exe)
    accept_followup_agreement(dev, serial, adb_exe)
    log(f"[{serial}] done")


def build_parser():
    parser = argparse.ArgumentParser(description="Fill login credentials from the Android UI hierarchy.")
    parser.add_argument("--device", required=True, help="ADB serial of the already-open device")
    parser.add_argument("--adb-executable", default=ADB_EXE, help="Path to adb executable")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.getLogger("airtest").setLevel(logging.WARNING)
    adb_executable = args.adb_executable
    if Path(adb_executable).is_file():
        adb_executable = str(Path(adb_executable).resolve())
    try:
        if not args.account or not args.password:
            raise ValueError("Provide --account and --password or set YUNANDROID_LOGIN_ACCOUNT and YUNANDROID_LOGIN_PASSWORD")
        process_device(args.device, args.account, args.password, adb_executable)
    except Exception as exc:
        print(f"[fatal] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

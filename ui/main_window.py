"""PySide6 desktop control panel for the cloud-phone connection queue."""

from __future__ import annotations

import ipaddress
import os
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.request import urlretrieve

from PySide6.QtCore import (
    QLibraryInfo,
    QSettings,
    QSignalBlocker,
    QThreadPool,
    QTimer,
    QTranslator,
    Qt,
    Signal,
)
from PySide6.QtGui import QIcon, QPainter, QPixmap, QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QApplication,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QMenu,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from services.device_operations import (
    ADB_PATH,
    DeviceOperations,
    LUA_DESTINATION_64BIT,
    LUA_DESTINATION_DEFAULT,
    SCRCPY_PATH,
    build_lua_target,
    calculate_scrcpy_window_layout,
    cleanup_json_pull_staging,
    validate_package_name,
    validate_remote_file_path,
)
from services.device_repository import (
    DEFAULT_PUBLIC_IP,
    PUBLIC_IP_PRESETS,
    QUEUE_FILE,
    Device,
    load_devices,
    next_device_id,
    normalize_public_ip,
    public_ip_options,
    renumber_local_ports as renumber_device_ports,
    save_devices,
)
from services.excel_summary_controller import ExcelSummaryController
from services.monitor_controller import MonitorController
from services.openSSH import TunnelManager, build_connection_batches
from services.screenshotUtil import build_output_identifier, safe_filename
import tools.package_inventory as package_inventory_module
from tools.package_inventory import PackageInventory
from workers.device_task_process import (
    process_spec_for_device_operation,
    process_spec_for_package_scan,
)
from workers.device_task_scheduler import DeviceTaskScheduler, device_resource_key
from workers.process_control import terminate_process_tree
from workers.task_worker import ProcessTaskSpec, TaskWorker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = PROJECT_ROOT / "config" / "ui_settings.ini"
MONITOR_CONFIG_FILE = PROJECT_ROOT / "config" / "monitor.ini"
DATA_DIR = PROJECT_ROOT / "data"
JSON_ARTIFACT = "json"
PULL_ARTIFACTS = (JSON_ARTIFACT, "dist.lua")
PULL_RESULT_DIR = DATA_DIR
JSON_OUTPUT_DIR = DATA_DIR / "outputs"
APK_DIR = DATA_DIR / "apks"
APK_TRANSFER_DIR = PROJECT_ROOT / "temp" / "apk_transfer"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
OSS_URL_FILE = PROJECT_ROOT / "config" / "oss_url.txt"
DEFAULT_LUA_PATH = PROJECT_ROOT / "tools" / "ChallengeLeaderMgr_XX.luac"
AIRTEST_SCRIPT_PATH = PROJECT_ROOT / "tools" / "airtest" / "airtest_login.py"
AIRTEST_AUTO_LOGIN_SCRIPT_PATH = PROJECT_ROOT / "tools" / "airtest" / "login_all_emulators.py"
_CHINESE_TRANSLATORS: list[QTranslator] = []


@dataclass
class _UiTaskGroup:
    group_id: int
    name: str
    total: int
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    on_finished: Callable[["_UiTaskGroup"], None] | None = None


def _safe_output_component(value: str, fallback: str) -> str:
    component = safe_filename(str(value).strip())
    component = "".join("_" if ord(char) < 32 else char for char in component)
    component = component.strip(". ")
    if not component or component in {".", ".."}:
        component = fallback
    reserved_stem = component.split(".", 1)[0].rstrip(". ").upper()
    if reserved_stem in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }:
        component = f"_{component}"
    return component


def build_json_result_directory(
    app_name: str,
    group: str,
    timestamp: datetime | None = None,
) -> Path:
    """Build one stable per-day JSON snapshot directory for a UI task."""
    moment = timestamp or datetime.now()
    safe_app_name = _safe_output_component(app_name, "unknown_app")
    safe_group = _safe_output_component(group, "unknown_group")
    return Path(JSON_OUTPUT_DIR) / moment.strftime("%Y%m%d") / f"{safe_app_name}_{safe_group}"


def install_chinese_translations(application: QApplication) -> bool:
    """Load Qt's bundled Simplified Chinese strings for standard dialog buttons."""
    if application.property("sfmanager_chinese_translations_installed"):
        return True
    translations_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    translator = QTranslator(application)
    if not translator.load("qtbase_zh_CN", translations_path):
        return False
    application.installTranslator(translator)
    application.setProperty("sfmanager_chinese_translations_installed", True)
    _CHINESE_TRANSLATORS.append(translator)
    return True


def build_pull_result_directory(
    artifact_type: str,
    app_name: str,
    package_name: str,
    timestamp: datetime | None = None,
    *,
    group: str = "",
    remote_ip: str = "",
) -> Path:
    """Build the per-device result directory for one supported artifact."""
    artifact = str(artifact_type).strip()
    if artifact not in PULL_ARTIFACTS:
        raise ValueError(f"不支持的拉取文件类型：{artifact}")
    if artifact == JSON_ARTIFACT:
        return build_json_result_directory(app_name, group, timestamp)
    root = Path(PULL_RESULT_DIR) / "lua"
    moment = timestamp or datetime.now()
    safe_app_name = safe_filename(str(app_name).strip()) or "unknown_app"
    safe_package_name = safe_filename(str(package_name).strip()) or "unknown_package"
    return root / f"{safe_app_name}_{safe_package_name}_{moment.strftime('%Y%m%d_%H%M%S')}"


class DeviceTableWidget(QTableWidget):
    """Device queue table with a paint-style checkbox selection gesture."""

    def __init__(self, rows: int, columns: int, parent: QWidget | None = None, *, checkbox_column: int = 0):
        super().__init__(rows, columns, parent)
        self._checkbox_column = checkbox_column
        self._checkbox_drag_active = False
        self._checkbox_drag_state = Qt.CheckState.Unchecked
        self._checkbox_drag_candidate_row: int | None = None
        self._checkbox_drag_last_row: int | None = None
        self._checkbox_drag_from_checkbox = False

    def _event_position(self, event):
        return event.position().toPoint()

    def _set_checkbox_state(self, row: int, state: Qt.CheckState):
        item = self.item(row, self._checkbox_column)
        if item is not None:
            item.setCheckState(state)

    def _start_checkbox_drag(self, row: int):
        item = self.item(row, self._checkbox_column)
        if item is None:
            return False
        self._checkbox_drag_active = True
        self._checkbox_drag_state = (
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        self._checkbox_drag_last_row = row
        return True

    def _apply_checkbox_drag_to(self, row: int):
        if self._checkbox_drag_last_row is None:
            return
        last_row = self._checkbox_drag_last_row
        step = 1 if row >= last_row else -1
        for crossed_row in range(last_row + step, row + step, step):
            self._set_checkbox_state(crossed_row, self._checkbox_drag_state)
        self._checkbox_drag_last_row = row

    def mousePressEvent(self, event):
        self._checkbox_drag_candidate_row = None
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(self._event_position(event))
            if index.isValid():
                self._checkbox_drag_candidate_row = index.row()
                if index.column() == self._checkbox_column and self._start_checkbox_drag(index.row()):
                    self._checkbox_drag_from_checkbox = True
                    self._set_checkbox_state(index.row(), self._checkbox_drag_state)
                    self.setCurrentCell(index.row(), self._checkbox_column)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._checkbox_drag_active:
            index = self.indexAt(self._event_position(event))
            if index.isValid():
                self._apply_checkbox_drag_to(index.row())
            if self._checkbox_drag_from_checkbox:
                event.accept()
                return
            super().mouseMoveEvent(event)
            event.accept()
            return

        if self._checkbox_drag_candidate_row is not None:
            index = self.indexAt(self._event_position(event))
            if index.isValid() and index.row() != self._checkbox_drag_candidate_row:
                start_row = self._checkbox_drag_candidate_row
                if self._start_checkbox_drag(start_row):
                    self._checkbox_drag_from_checkbox = False
                    self._set_checkbox_state(start_row, self._checkbox_drag_state)
                    self._apply_checkbox_drag_to(index.row())
                    super().mouseMoveEvent(event)
                    event.accept()
                    return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._checkbox_drag_active and event.button() == Qt.MouseButton.LeftButton:
            from_checkbox = self._checkbox_drag_from_checkbox
            self._checkbox_drag_active = False
            self._checkbox_drag_candidate_row = None
            self._checkbox_drag_last_row = None
            self._checkbox_drag_from_checkbox = False
            if from_checkbox:
                event.accept()
                return
            super().mouseReleaseEvent(event)
            return
        self._checkbox_drag_candidate_row = None
        super().mouseReleaseEvent(event)


class TaskStatusLabel(QLabel):
    """Status label that lets the dashboard mirror task transitions."""

    text_changed = Signal(str)

    def setText(self, text: str):
        super().setText(text)
        self.text_changed.emit(str(text))


class ScrollableFilterComboBox(QComboBox):
    """Expose a scrollbar whenever the popup cannot show every filter value."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMaxVisibleItems(40)
        self.setStyleSheet("QComboBox { combobox-popup: 0; }")

    def showPopup(self):
        super().showPopup()
        self.view().setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)


class MainWindow(QMainWindow):
    scrcpy_status_reported = Signal(object)
    apk_transfer_progress = Signal(str)

    SCRCPY_TOP_MARGIN = 24
    SCRCPY_BOTTOM_MARGIN = 24
    CHECK_COLUMN = 0
    SEQUENCE_COLUMN = 1
    APP_NAME_COLUMN = 2
    GROUP_COLUMN = 3
    PUBLIC_IP_COLUMN = 4
    REMOTE_ADDRESS_COLUMN = 5
    LOCAL_PORT_COLUMN = 6
    NOTE_COLUMN = 7
    PACKAGE_NAME_COLUMN = 8
    TAG_COLUMN = 9
    GROUP_HEADER = "区组"
    FILTER_ALL = "__all__"
    FILTER_EMPTY = "__empty__"
    TAG_FILTER_ALL = "__all__"
    TAG_FILTER_EMPTY = "__empty__"
    TAG_FILTER_ALL_LABEL = "\u5168\u90e8\u6807\u7b7e"
    TAG_FILTER_EMPTY_LABEL = "\u65e0\u6807\u7b7e"
    PUBLIC_IP_SERVER_NAMES = {
        PUBLIC_IP_PRESETS[0]: "\u4e91\u670d\u52a1\u5668 1",
        PUBLIC_IP_PRESETS[1]: "\u4e91\u670d\u52a1\u5668 2",
    }
    SCRCPY_STATUS_PENDING = "\u5f85\u542f\u52a8"
    SCRCPY_STATUS_POPEN = "Popen \u6210\u529f"
    SCRCPY_STATUS_ADB_READY = "ADB \u5c31\u7eea"
    SCRCPY_STATUS_STABLE = "进程存活（待验窗口）"
    SCRCPY_STATUS_FAILED = "\u5931\u8d25"
    TAG_HEADER = "\u6807\u7b7e"
    TABLE_HEADERS = ["选择", "序号", "应用名", GROUP_HEADER, "远端地址", "本地端口", "备注", "包名", TAG_HEADER]
    TABLE_HEADERS.insert(PUBLIC_IP_COLUMN, "\u516c\u7f51 IP")
    FILTERABLE_COLUMNS = (
        APP_NAME_COLUMN,
        GROUP_COLUMN,
        PUBLIC_IP_COLUMN,
        REMOTE_ADDRESS_COLUMN,
        LOCAL_PORT_COLUMN,
        NOTE_COLUMN,
        PACKAGE_NAME_COLUMN,
        TAG_COLUMN,
    )

    def __init__(
        self,
        devices: list[Device] | None = None,
        operations: DeviceOperations | None = None,
        tunnel_manager: TunnelManager | None = None,
        package_inventory: PackageInventory | None = None,
        monitor_controller: MonitorController | None = None,
        excel_summary_controller: ExcelSummaryController | None = None,
        queue_file: str | Path | None = None,
        settings_file: str | Path | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("\u4e91\u624b\u673a\u7ba1\u7406")
        self.resize(1280, 820)
        self.setMinimumSize(1080, 700)

        self.queue_file = Path(queue_file) if queue_file is not None else QUEUE_FILE
        self.settings_file = Path(settings_file) if settings_file is not None else SETTINGS_FILE
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self._settings = QSettings(str(self.settings_file), QSettings.Format.IniFormat)
        self.devices = devices if devices is not None else self._load_devices_safely()
        self.operations = operations or DeviceOperations()
        self.tunnel_manager = tunnel_manager or TunnelManager(adb_executable=str(ADB_PATH))
        self.package_inventory = package_inventory or PackageInventory(
            adb_path=ADB_PATH,
            ssh_user_host=self.tunnel_manager.ssh_user_host,
            pem_path=self.tunnel_manager.pem_path,
            tunnel_wait=self.tunnel_manager.tunnel_wait,
        )
        self.monitor_controller = monitor_controller or MonitorController(MONITOR_CONFIG_FILE)
        self.excel_summary_controller = excel_summary_controller or ExcelSummaryController(
            MONITOR_CONFIG_FILE
        )
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(4)
        self._task_scheduler = DeviceTaskScheduler()
        self._task_groups: dict[int, _UiTaskGroup] = {}
        self._next_task_group_id = 1
        self._next_task_job_id = 1
        self._job_groups: dict[int, int] = {}
        self._job_scheduler_tasks: dict[int, int] = {}
        self._scheduler_jobs: dict[int, int] = {}
        self._job_workers: dict[int, TaskWorker] = {}
        self._worker_jobs: dict[TaskWorker, int] = {}
        self._workers: set[TaskWorker] = set()
        self._task_processes: dict[TaskWorker, int] = {}
        self._task_total = 0
        self._task_finished = 0
        self._active_task_name = ""
        self._operation_buttons: list[QPushButton] = []
        self._connection_worker: TaskWorker | None = None
        self._monitor_status_worker: TaskWorker | None = None
        self._monitor_control_worker: TaskWorker | None = None
        self._excel_summary_status_worker: TaskWorker | None = None
        self._excel_summary_control_worker: TaskWorker | None = None
        self.scrcpy_launch_delay_ms = 800
        self.scrcpy_startup_check_ms = 500
        self.scrcpy_stability_check_ms = 1000
        self.scrcpy_stability_poll_ms = 100
        self.scrcpy_diagnostic_delay_ms = 5000
        self.scrcpy_retry_delay_ms = 500
        self.scrcpy_max_retries = 2
        self._scrcpy_queue: list[Device] = []
        self._scrcpy_queue_worker: TaskWorker | None = None
        self._scrcpy_statuses: dict[str, str] = {}
        self._adb_shell_queue: list[Device] = []
        self._adb_shell_queue_worker: TaskWorker | None = None
        self._lua_queue: list[tuple[Device, str]] = []
        self._lua_queue_worker: TaskWorker | None = None
        self._lua_queue_path = ""
        self._lua_queue_destination = LUA_DESTINATION_DEFAULT
        self._pull_queue: list[Device] = []
        self._pull_queue_worker: TaskWorker | None = None
        self._pull_queue_artifact = ""
        self._pull_queue_started_at: datetime | None = None
        self._pull_queue_failed = False
        self._package_scan_worker: TaskWorker | None = None
        self._package_scan_failed_state = False
        self._apk_pending: deque[tuple[Device, str]] = deque()
        self._apk_worker_tasks: dict[TaskWorker, tuple[Device, str]] = {}
        self._apk_active = 0
        self._apk_completed = 0
        self._apk_cancelled = 0
        self._apk_successes: list[dict[str, str]] = []
        self._apk_failures: list[dict[str, str]] = []
        self._apk_selected_ids: set[str] = set()
        self._move_apk_worker: TaskWorker | None = None
        self._move_apk_source: Device | None = None
        self._move_apk_target: Device | None = None
        self._move_apk_failed_state = False
        self._stop_requested = False
        self._public_ip_view: str | None = None
        self._startup_error = getattr(self, "_startup_error", "")
        self.scrcpy_status_reported.connect(self._scrcpy_status_updated)
        self.apk_transfer_progress.connect(self._append_log)

        self._build_ui()
        self._populate_devices()
        self._refresh_active_connections()
        self._append_log(f"已加载 {len(self.devices)} 条连接记录")
        if self._startup_error:
            self._append_log(f"连接队列加载失败：{self._startup_error}")
        self._monitor_status_timer = QTimer(self)
        self._monitor_status_timer.setInterval(2000)
        self._monitor_status_timer.timeout.connect(self._refresh_monitor_status)
        self._monitor_status_timer.timeout.connect(self._refresh_excel_summary_status)
        self._monitor_status_timer.start()
        QTimer.singleShot(0, self._refresh_monitor_status)
        QTimer.singleShot(0, self._refresh_excel_summary_status)

    def _load_devices_safely(self) -> list[Device]:
        try:
            return load_devices(self.queue_file)
        except (FileNotFoundError, ValueError) as exc:
            self._startup_error = str(exc)
            return []

    def _persist_devices(
        self,
        devices: list[Device],
        *,
        checked_ids: set[str] | None = None,
    ) -> bool:
        """Persist the Excel queue and refresh the in-memory list from that file."""
        save_devices(self.queue_file, devices)
        self.devices = load_devices(self.queue_file)
        if hasattr(self, "public_ip_selector"):
            self._refresh_public_ip_options()
        self._populate_devices(checked_ids)
        return True

    def _build_ui(self):
        central = QWidget(self)
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)

        header_frame = QFrame()
        header_frame.setObjectName("appHeader")
        header = QHBoxLayout(header_frame)
        header.setContentsMargins(18, 14, 18, 14)
        header.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("云手机控制台")
        title.setObjectName("pageTitle")
        subtitle = QLabel("设备连接与批量任务中心")
        subtitle.setObjectName("pageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        self.header_total_value = self._dashboard_metric(header, "总设备", "headerMetricValue")
        self.header_online_value = self._dashboard_metric(header, "在线设备", "headerOnlineValue")
        self.header_task_value = self._dashboard_metric(header, "当前任务", "headerTaskValue", "就绪")
        header.addSpacing(4)
        self.reload_button = QPushButton("刷新队列")
        self.reload_button.setObjectName("secondaryButton")
        self.reload_button.clicked.connect(self.reload_queue)
        self.open_folder_button = QPushButton("打开配置目录")
        self.open_folder_button.setObjectName("secondaryButton")
        self.open_folder_button.clicked.connect(self.open_config_folder)
        header.addWidget(self.open_folder_button)
        self.open_data_folder_button = QPushButton("打开数据目录")
        self.open_data_folder_button.setObjectName("openDataFolderButton")
        self.open_data_folder_button.clicked.connect(self.open_data_folder)
        header.addWidget(self.open_data_folder_button)
        root.addWidget(header_frame)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("mainSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_queue_panel())
        action_scroll = QScrollArea()
        action_scroll.setObjectName("actionScrollArea")
        action_scroll.setWidgetResizable(True)
        action_scroll.setFrameShape(QFrame.Shape.NoFrame)
        action_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        action_scroll.setWidget(self._build_action_panel())
        splitter.addWidget(action_scroll)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 5)
        root.addWidget(splitter, 1)

        output_group = QGroupBox("任务输出")
        output_layout = QVBoxLayout(output_group)
        output_layout.setContentsMargins(14, 12, 14, 12)
        status_row = QHBoxLayout()
        self.task_status = TaskStatusLabel("就绪")
        self.task_status.setObjectName("taskStatus")
        self.task_status.text_changed.connect(self._update_header_task)
        self.task_progress = QProgressBar()
        self.task_progress.setObjectName("taskProgress")
        self.task_progress.setRange(0, 1)
        self.task_progress.setValue(0)
        self.stop_task_button = QPushButton("终止当前任务")
        self.stop_task_button.setObjectName("stopTaskButton")
        self.stop_task_button.setEnabled(False)
        self.stop_task_button.clicked.connect(self.stop_current_task)
        status_row.addWidget(self.task_status)
        status_row.addWidget(self.task_progress, 1)
        status_row.addWidget(self.stop_task_button)
        output_layout.addLayout(status_row)
        self.task_log = QPlainTextEdit()
        self.task_log.setObjectName("taskLog")
        self.task_log.setReadOnly(True)
        self.task_log.setMaximumBlockCount(2000)
        output_layout.addWidget(self.task_log)
        root.addWidget(output_group, 0)

        self.setStyleSheet(
            """
            QMainWindow, #centralWidget { background: #eef2f6; }
            QFrame#appHeader { background: #101827; border: 1px solid #25334a; border-radius: 10px; }
            QLabel#pageTitle { color: #f8fafc; font-size: 24px; font-weight: 700; }
            QLabel#pageSubtitle { color: #91a1b8; font-size: 11px; }
            QFrame#headerMetric { background: #182337; border: 1px solid #2b3a54; border-radius: 7px; }
            QLabel#headerMetricLabel { color: #91a1b8; font-size: 10px; font-weight: 600; }
            QLabel#headerMetricValue { color: #ffffff; font-size: 20px; font-weight: 700; }
            QLabel#headerOnlineValue { color: #55d6a3; font-size: 20px; font-weight: 700; }
            QLabel#headerTaskValue { color: #9cc7ff; font-size: 12px; font-weight: 700; }
            QGroupBox { background: #ffffff; border: 1px solid #d5dde7; border-radius: 8px; margin-top: 10px; padding-top: 12px; font-weight: 600; color: #203047; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
            QFrame#connectionPanel { background: #f8fafc; border: 1px solid #d5dde7; border-radius: 7px; }
            QTableWidget { background: #ffffff; border: 1px solid #d5dde7; gridline-color: #e7ecf2; selection-background-color: #dcecff; selection-color: #162337; alternate-background-color: #f8fafc; }
            QTableWidget::indicator { width: 20px; height: 20px; }
            QTableWidget::item:hover { background: #eef6ff; }
            QHeaderView::section { background: #e8eef5; color: #42536b; border: none; border-bottom: 1px solid #d5dde7; padding: 8px; font-weight: 700; }
            QTableWidget::item { padding: 6px; }
            QPushButton { min-height: 34px; border: 1px solid #c9d5d8; border-radius: 5px; padding: 0 14px; background: #ffffff; color: #23434a; font-weight: 600; }
            QPushButton:hover { border-color: #aebec2; background: #ffffff; }
            QPushButton:pressed { background: #f5f7f7; }
            QPushButton:disabled { background: #ffffff; color: #9aa8ab; border-color: #d8e0e2; }
            QPushButton#primaryButton, QPushButton#pullArtifactButton, QPushButton#connectButton,
            QPushButton#dangerButton, QPushButton#stopTaskButton, QPushButton#secondaryButton,
            QPushButton#openDataFolderButton, QPushButton#disconnectButton, QPushButton#renumberPortsButton,
            QPushButton#autoLoginButton, QPushButton#arrangeScrcpyButton, QPushButton#closeScrcpyButton,
            QPushButton#adbShellButton, QPushButton#scanPackagesButton { background: #ffffff; border-color: #c9d5d8; color: #23434a; }
            QPushButton#primaryButton:hover, QPushButton#pullArtifactButton:hover, QPushButton#connectButton:hover,
            QPushButton#dangerButton:hover, QPushButton#stopTaskButton:hover, QPushButton#secondaryButton:hover,
            QPushButton#openDataFolderButton:hover, QPushButton#disconnectButton:hover, QPushButton#renumberPortsButton:hover,
            QPushButton#autoLoginButton:hover, QPushButton#arrangeScrcpyButton:hover, QPushButton#closeScrcpyButton:hover,
            QPushButton#adbShellButton:hover, QPushButton#scanPackagesButton:hover { background: #ffffff; border-color: #aebec2; color: #23434a; }
            QLineEdit, QSpinBox, QComboBox { min-height: 32px; border: 1px solid #cbd6e2; border-radius: 5px; padding: 0 8px; background: #ffffff; color: #203047; }
            QPlainTextEdit { border: 1px solid #27364d; border-radius: 6px; background: #101827; color: #c9d7ea; font-family: Consolas, monospace; }
            QProgressBar { min-height: 18px; border: 1px solid #cbd6e2; border-radius: 5px; background: #e8eef5; text-align: center; color: #203047; }
            QProgressBar::chunk { background: #55d6a3; border-radius: 4px; }
            QToolButton { border: 1px solid #c9d5d8; border-radius: 5px; padding: 5px 10px; background: #ffffff; color: #23434a; font-weight: 600; }
            QToolButton:hover { border-color: #aebec2; background: #ffffff; color: #23434a; }
            QToolButton:pressed, QToolButton:checked { background: #f5f7f7; }
            """
        )

    @staticmethod
    def _dashboard_metric(header: QHBoxLayout, label: str, value_object_name: str, value: str = "0") -> QLabel:
        frame = QFrame()
        frame.setObjectName("headerMetric")
        frame.setMinimumWidth(82 if value_object_name != "headerTaskValue" else 132)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(0)
        caption = QLabel(label)
        caption.setObjectName("headerMetricLabel")
        metric = QLabel(value)
        metric.setObjectName(value_object_name)
        metric.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(caption)
        layout.addWidget(metric)
        header.addWidget(frame)
        return metric

    def _update_header_task(self, text: str | None = None):
        if hasattr(self, "header_task_value"):
            self.header_task_value.setText(str(text if text is not None else self.task_status.text()))

    def _update_dashboard_metrics(self):
        if hasattr(self, "header_total_value"):
            self.header_total_value.setText(str(len(self.devices)))
        if hasattr(self, "header_online_value"):
            active_serials = {
                f"127.0.0.1:{mapping.get('local_port')}"
                for mapping in self._active_mappings()
                if mapping.get("local_port") is not None
            }
            online = sum(1 for device in self.devices if device.serial in active_serials)
            self.header_online_value.setText(str(online))

    def _build_queue_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("连接队列"))
        self.device_count = QLabel("0 条")
        self.device_count.setStyleSheet("color: #66777c;")
        toolbar.addWidget(self.device_count)
        toolbar.addStretch()
        self.reload_button.setObjectName("refreshDevicesButton")
        self.add_record_button = QPushButton("新增记录")
        self.add_record_button.clicked.connect(self.add_record)
        self.save_record_button = QPushButton("保存修改")
        self.save_record_button.clicked.connect(self.save_queue)
        self.delete_record_button = QPushButton("删除记录")
        self.delete_record_button.setObjectName("dangerButton")
        self.delete_record_button.clicked.connect(self.delete_records)
        toolbar.addWidget(self.reload_button)
        toolbar.addWidget(self.add_record_button)
        toolbar.addWidget(self.save_record_button)
        toolbar.addWidget(self.delete_record_button)
        layout.addLayout(toolbar)

        view_toolbar = QHBoxLayout()
        self.public_ip_view_button = QToolButton()
        self.public_ip_view_button.setObjectName("publicIpViewButton")
        self.public_ip_view_button.setMinimumWidth(150)
        self.public_ip_view_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.public_ip_view_menu = QMenu(self.public_ip_view_button)
        self.public_ip_view_button.setMenu(self.public_ip_view_menu)
        self._refresh_public_ip_view_options()
        view_toolbar.addWidget(self.public_ip_view_button)
        self.filter_label = QLabel("筛选")
        self.filter_column = QComboBox()
        self.filter_column.setObjectName("filterColumn")
        self.filter_column.setMinimumWidth(90)
        for column in self.FILTERABLE_COLUMNS:
            self.filter_column.addItem(self.TABLE_HEADERS[column], column)
        self.filter_column.setCurrentIndex(self.filter_column.findData(self.TAG_COLUMN))
        self.filter_value = ScrollableFilterComboBox()
        self.filter_value.setObjectName("tagFilter")
        self.filter_value.setMinimumWidth(130)
        self.filter_column.currentIndexChanged.connect(self._filter_column_changed)
        self.filter_value.currentIndexChanged.connect(self._apply_column_filter)
        self.device_search = QLineEdit()
        self.device_search.setObjectName("deviceSearch")
        self.device_search.setPlaceholderText("搜索设备名称、IP、包名…")
        self.device_search.setClearButtonEnabled(True)
        self.device_search.setMinimumWidth(220)
        self.device_search.textChanged.connect(self._apply_column_filter)
        # Compatibility aliases for the former tag-only filter API.
        self.tag_filter_label = self.filter_label
        self.tag_filter = self.filter_value
        view_toolbar.addWidget(self.filter_label)
        view_toolbar.addWidget(self.filter_column)
        view_toolbar.addWidget(self.filter_value)
        view_toolbar.addWidget(self.device_search)
        view_toolbar.addStretch()
        self.select_all_devices = QCheckBox("全选")
        self.select_all_devices.setObjectName("selectAllDevicesCheckBox")
        self.select_all_devices.setTristate(True)
        self.select_all_devices.stateChanged.connect(self._select_all_state_changed)
        view_toolbar.addWidget(self.select_all_devices)
        layout.addLayout(view_toolbar)

        layout.addWidget(self._build_connection_panel())

        self.device_table = DeviceTableWidget(
            0,
            len(self.TABLE_HEADERS),
            panel,
            checkbox_column=self.CHECK_COLUMN,
        )
        self.device_table.setObjectName("deviceTable")
        self.device_table.setHorizontalHeaderLabels(self.TABLE_HEADERS)
        self.device_table.setAlternatingRowColors(True)
        self.device_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.device_table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed
        )
        self.device_table.verticalHeader().setVisible(False)
        self.device_table.setCornerButtonEnabled(False)
        header = self.device_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(self.CHECK_COLUMN, header.ResizeMode.Fixed)
        header.setSectionResizeMode(self.SEQUENCE_COLUMN, header.ResizeMode.Fixed)
        header.setSectionResizeMode(self.APP_NAME_COLUMN, header.ResizeMode.Stretch)
        header.setSectionResizeMode(self.GROUP_COLUMN, header.ResizeMode.Fixed)
        header.setSectionResizeMode(self.PUBLIC_IP_COLUMN, header.ResizeMode.Fixed)
        header.setSectionResizeMode(self.REMOTE_ADDRESS_COLUMN, header.ResizeMode.Fixed)
        header.setSectionResizeMode(self.LOCAL_PORT_COLUMN, header.ResizeMode.Fixed)
        header.setSectionResizeMode(self.NOTE_COLUMN, header.ResizeMode.Stretch)
        header.setSectionResizeMode(self.PACKAGE_NAME_COLUMN, header.ResizeMode.Stretch)
        self.device_table.setColumnWidth(self.CHECK_COLUMN, 68)
        self.device_table.setColumnWidth(self.SEQUENCE_COLUMN, 52)
        self.device_table.setColumnWidth(self.GROUP_COLUMN, 90)
        self.device_table.setColumnWidth(self.PUBLIC_IP_COLUMN, 128)
        self.device_table.setColumnWidth(self.REMOTE_ADDRESS_COLUMN, 145)
        self.device_table.setColumnWidth(self.LOCAL_PORT_COLUMN, 82)
        self.device_table.setColumnWidth(self.TAG_COLUMN, 105)
        header.setSectionResizeMode(self.TAG_COLUMN, header.ResizeMode.Fixed)
        self.device_table.itemChanged.connect(self._device_table_item_changed)
        self.device_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.device_table.customContextMenuRequested.connect(self._show_device_context_menu)
        viewport = self.device_table.viewport()
        viewport.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        viewport.customContextMenuRequested.connect(self._show_device_context_menu)
        layout.addWidget(self.device_table, 1)
        return panel

    def _build_device_context_menu(self, row: int) -> QMenu:
        menu = QMenu(self)
        if row < 0 or row >= len(self.devices):
            return menu
        device = self.devices[row]
        actions = [
            ("连接", self.connect_tunnels, self.connect_button),
            ("断开", self.disconnect_tunnels, self.disconnect_button),
            ("安装 APK", self.install_apk, self.install_button),
            ("移动 APK", self.move_apk, self.install_button),
            ("运行 Airtest", self.run_airtest, self.airtest_button),
            ("自动填充登录", self.run_auto_login, self.auto_login_button),
            ("截图", self.take_screenshots, self.screenshot_button),
            ("推送 Lua", self.push_lua, self.lua_button),
            ("重启应用", self.restart_apps, self.restart_button),
            ("Scrcpy", self.start_scrcpy, self.scrcpy_button),
            ("扫描三方包", self.scan_packages, self.scan_packages_button),
            ("ADB Shell", self.start_adb_shell, self.adb_shell_button),
            ("清理第三方包", self.clear_packages, self.clear_button),
            ("清理日期 JSON", self.clear_sf_json, self.adb_shell_button),
            ("推送文件", self.push_file, self.adb_shell_button),
        ]
        for label, handler, source_button in actions:
            action = menu.addAction(label)
            action.setEnabled(source_button.isEnabled())
            action.triggered.connect(
                lambda _checked=False, handler=handler, device=device: handler(devices=[device])
            )
        return menu

    def _show_device_context_menu(self, position):
        index = self.device_table.indexAt(position)
        if not index.isValid():
            return
        self.device_table.setCurrentCell(index.row(), index.column())
        menu = self._build_device_context_menu(index.row())
        menu.exec(self.device_table.viewport().mapToGlobal(position))

    def _build_connection_panel(self) -> QWidget:
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(4)

        self.connection_toggle = QToolButton()
        self.connection_toggle.setText("连接参数")
        self.connection_toggle.setCheckable(True)
        self.connection_toggle.setChecked(True)
        self.connection_toggle.setArrowType(Qt.ArrowType.DownArrow)
        self.connection_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.connection_toggle.toggled.connect(self._toggle_connection_panel)
        wrapper_layout.addWidget(self.connection_toggle, 0, Qt.AlignmentFlag.AlignLeft)

        self.connection_panel = QFrame()
        self.connection_panel.setObjectName("connectionPanel")
        self.connection_panel.setFrameShape(QFrame.Shape.StyledPanel)
        panel_layout = QVBoxLayout(self.connection_panel)
        panel_layout.setContentsMargins(10, 8, 10, 10)
        panel_layout.setSpacing(7)
        form = QFormLayout()
        form.setVerticalSpacing(6)
        self.public_ip_selector = QComboBox()
        self.public_ip_selector.setObjectName("publicIpSelector")
        self.public_ip_selector.setEditable(True)
        self.public_ip_selector.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.start_local_port = QLineEdit()
        self.start_local_port.setObjectName("startLocalPort")
        self.start_local_port.setPlaceholderText("例如 63000, 63002-63005")
        self.loop_count = QLineEdit()
        self.loop_count.setObjectName("loopCount")
        self.loop_count.setPlaceholderText("例如 2")
        self.start_ip_first = QLineEdit()
        self.start_ip_first.setObjectName("startIpFirst")
        self.start_ip_first.setPlaceholderText("例如 2, 78-81")
        form.addRow("\u516c\u7f51 IP", self.public_ip_selector)
        self._apply_saved_connection_values()
        self.public_ip_selector.currentTextChanged.connect(self._save_connection_values)
        self.start_local_port.textChanged.connect(self._save_connection_values)
        self.loop_count.textChanged.connect(self._save_connection_values)
        self.start_ip_first.textChanged.connect(self._save_connection_values)
        form.addRow("起始本地端口", self.start_local_port)
        form.addRow("循环次数", self.loop_count)
        form.addRow("起始 IP 末段", self.start_ip_first)
        panel_layout.addLayout(form)

        generate_row = QHBoxLayout()
        self.generate_queue_button = QPushButton("生成队列")
        self.generate_queue_button.setObjectName("primaryButton")
        self.generate_queue_button.clicked.connect(self.generate_queue)
        self.connect_button = QPushButton("建立连接")
        self.connect_button.setObjectName("connectButton")
        self.connect_button.clicked.connect(self.connect_tunnels)
        self.disconnect_button = QPushButton("断开连接")
        self.disconnect_button.setObjectName("disconnectButton")
        self.disconnect_button.clicked.connect(self.disconnect_tunnels)
        self.renumber_ports_button = QPushButton("按起始端口重排")
        self.renumber_ports_button.setObjectName("renumberPortsButton")
        self.renumber_ports_button.setToolTip("有活动连接时请先点击“断开连接”，再从起始本地端口按顺序重排队列")
        self.renumber_ports_button.clicked.connect(self.renumber_local_ports)
        generate_row.addWidget(self.generate_queue_button)
        generate_row.addWidget(self.connect_button)
        generate_row.addWidget(self.disconnect_button)
        generate_row.addWidget(self.renumber_ports_button)
        panel_layout.addLayout(generate_row)
        wrapper_layout.addWidget(self.connection_panel)
        return wrapper

    def _build_action_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(500)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._build_monitor_group())
        layout.addWidget(self._build_excel_summary_group())

        action_group = QGroupBox("批量操作")
        action_group.setObjectName("actionGroup")
        action_layout = QVBoxLayout(action_group)
        action_layout.setContentsMargins(18, 20, 18, 18)
        grid = QGridLayout()
        grid.setObjectName("operationGrid")
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        self.install_button = QPushButton("安装 APK")
        self.install_button.setObjectName("primaryButton")
        self.install_button.clicked.connect(self.install_apk)
        self.airtest_button = QPushButton("运行 Airtest")
        self.airtest_button.setObjectName("primaryButton")
        self.airtest_button.clicked.connect(self.run_airtest)
        self.auto_login_button = QPushButton("自动填充登录")
        self.auto_login_button.setObjectName("autoLoginButton")
        self.auto_login_button.setToolTip("识别账号、密码和确认按钮后自动输入测试凭据")
        self.auto_login_button.clicked.connect(self.run_auto_login)
        self.clear_button = QPushButton("清理第三方包")
        self.clear_button.setObjectName("dangerButton")
        self.clear_button.clicked.connect(self.clear_packages)
        self.screenshot_button = QPushButton("批量截图")
        self.screenshot_button.setObjectName("primaryButton")
        self.screenshot_button.clicked.connect(self.take_screenshots)
        self.lua_button = QPushButton("推送 Lua")
        self.lua_button.setObjectName("primaryButton")
        self.lua_button.clicked.connect(self.push_lua)
        self.pull_artifact_button = QPushButton("拉取文件")
        self.pull_artifact_button.setObjectName("pullArtifactButton")
        self.pull_artifact_button.clicked.connect(self.pull_artifact)
        self.restart_button = QPushButton("重启应用")
        self.restart_button.setObjectName("primaryButton")
        self.restart_button.clicked.connect(self.restart_apps)
        self.scrcpy_button = QPushButton("Scrcpy")
        self.scrcpy_button.clicked.connect(self.start_scrcpy)
        self.arrange_scrcpy_button = QPushButton("自动排列 Scrcpy")
        self.arrange_scrcpy_button.setObjectName("arrangeScrcpyButton")
        self.arrange_scrcpy_button.setToolTip("按每行数量从屏幕顶部平铺已打开的 Scrcpy 窗口")
        self.arrange_scrcpy_button.clicked.connect(self.arrange_scrcpy_windows)
        self.close_scrcpy_button = QPushButton("关闭全部 Scrcpy")
        self.close_scrcpy_button.setObjectName("closeScrcpyButton")
        self.close_scrcpy_button.setToolTip("关闭本程序启动的所有 Scrcpy 窗口")
        self.close_scrcpy_button.clicked.connect(self.close_scrcpy_windows)
        self.adb_shell_button = QPushButton("ADB Shell")
        self.adb_shell_button.setObjectName("adbShellButton")
        self.adb_shell_button.setToolTip("先执行 adb root，再在 temp 目录打开交互式 shell")
        self.adb_shell_button.clicked.connect(self.start_adb_shell)
        self.scan_packages_button = QPushButton()
        self.scan_packages_button.setObjectName("scanPackagesButton")
        self.scan_packages_button.setText("\u626b\u63cf\u4e09\u65b9\u5305")
        self.scan_packages_button.setToolTip("扫描选中设备的第三方应用并回写队列")
        self.scan_packages_button.clicked.connect(self.scan_packages)
        self._operation_buttons = [
            self.install_button,
            self.airtest_button,
            self.auto_login_button,
            self.clear_button,
            self.screenshot_button,
            self.lua_button,
            self.pull_artifact_button,
            self.restart_button,
            self.scrcpy_button,
            self.arrange_scrcpy_button,
            self.close_scrcpy_button,
            self.adb_shell_button,
            self.scan_packages_button,
        ]
        for index, button in enumerate(self._operation_buttons):
            button.setFixedHeight(40)
            grid.addWidget(button, index // 2, index % 2)
        self._operation_buttons.append(self.renumber_ports_button)
        self.renumber_ports_button.setFixedHeight(40)
        action_layout.addLayout(grid)
        layout.addWidget(action_group)

        files_group = QGroupBox("任务参数")
        form = QFormLayout(files_group)
        form.setContentsMargins(14, 16, 14, 14)
        form.setVerticalSpacing(9)
        self.apk_path = QLineEdit(str(OSS_URL_FILE))
        self.apk_path.setObjectName("ossUrlPath")
        self.apk_path.setReadOnly(True)
        form.addRow("OSS URL 文件", self.apk_path)
        self.lua_path = self._path_row(
            form,
            "Lua 文件",
            str(DEFAULT_LUA_PATH.parent),
            "Lua (*.lua *.luac *.luacc);;所有文件 (*)",
        )
        self._apply_saved_lua_path()
        self.lua_path.textChanged.connect(self._save_lua_path)
        self.lua_destination_selector = QComboBox()
        self.lua_destination_selector.setObjectName("luaDestinationSelector")
        self.lua_destination_selector.addItem("默认路径", LUA_DESTINATION_DEFAULT)
        self.lua_destination_selector.addItem("64 位路径", LUA_DESTINATION_64BIT)
        self.lua_destination_selector.setItemData(
            0,
            "/data/data/{包名}/files/atmu/src/mgr/ChallengeLeaderMgr.luac",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.lua_destination_selector.setItemData(
            1,
            "/data/data/{包名}/files/atmu/src/64bit/mgr/ChallengeLeaderMgr.luac",
            Qt.ItemDataRole.ToolTipRole,
        )
        form.addRow("Lua 推送路径", self.lua_destination_selector)
        self.pull_artifact_selector = QComboBox()
        self.pull_artifact_selector.setObjectName("pullArtifactSelector")
        self.pull_artifact_selector.addItems(list(PULL_ARTIFACTS))
        form.addRow("拉取文件类型", self.pull_artifact_selector)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 16)
        self.concurrency.setValue(3)
        self.scrcpy_columns = QSpinBox()
        self.scrcpy_columns.setObjectName("scrcpyColumns")
        self.scrcpy_columns.setRange(1, 25)
        self.scrcpy_columns.setValue(5)
        self.scrcpy_columns.setToolTip("Scrcpy 窗口每行排列的数量")
        form.addRow("Scrcpy 每行", self.scrcpy_columns)
        form.addRow("并发数", self.concurrency)
        layout.addWidget(files_group)

        layout.addStretch()
        panel.setMinimumHeight(panel.sizeHint().height())
        return panel

    def _build_monitor_group(self) -> QGroupBox:
        monitor_group = QGroupBox("JSON 停更监控")
        monitor_layout = QVBoxLayout(monitor_group)
        monitor_layout.setContentsMargins(14, 16, 14, 14)
        monitor_layout.setSpacing(9)
        monitor_header = QHBoxLayout()
        self.monitor_status = QLabel("正在检查状态")
        self.monitor_status.setObjectName("monitorStatus")
        self.monitor_status.setWordWrap(True)
        self.monitor_toggle = QCheckBox("启用监控")
        self.monitor_toggle.setObjectName("monitorToggle")
        self.monitor_toggle.toggled.connect(self._monitor_toggled)
        monitor_header.addWidget(self.monitor_status, 1)
        monitor_header.addWidget(self.monitor_toggle)
        monitor_layout.addLayout(monitor_header)

        monitor_actions = QHBoxLayout()
        self.monitor_run_now_button = QPushButton("立即巡检")
        self.monitor_run_now_button.setObjectName("monitorRunNowButton")
        self.monitor_run_now_button.setEnabled(False)
        self.monitor_run_now_button.clicked.connect(self._monitor_run_now)
        self.monitor_config_button = QPushButton("打开监控配置")
        self.monitor_config_button.setObjectName("monitorConfigButton")
        self.monitor_config_button.clicked.connect(self.open_monitor_config)
        monitor_actions.addWidget(self.monitor_run_now_button)
        monitor_actions.addWidget(self.monitor_config_button)
        monitor_actions.addStretch()
        monitor_layout.addLayout(monitor_actions)
        return monitor_group

    def _refresh_monitor_status(self):
        if self._monitor_status_worker is not None:
            return
        worker = TaskWorker(self.monitor_controller.status)
        self._monitor_status_worker = worker
        worker.signals.succeeded.connect(self._apply_monitor_status)
        worker.signals.failed.connect(
            lambda error: self._apply_monitor_status(
                {"running": False, "status": "error", "error": error}
            )
        )
        worker.signals.finished.connect(
            lambda worker=worker: self._monitor_status_finished(worker)
        )
        self.thread_pool.start(worker)

    def _build_excel_summary_group(self) -> QGroupBox:
        group = QGroupBox("Excel 日报汇总")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(9)

        header = QHBoxLayout()
        self.excel_summary_status = QLabel("正在检查状态")
        self.excel_summary_status.setObjectName("excelSummaryStatus")
        self.excel_summary_status.setWordWrap(True)
        self.excel_summary_toggle = QCheckBox("启用汇总")
        self.excel_summary_toggle.setObjectName("excelSummaryToggle")
        self.excel_summary_toggle.toggled.connect(self._excel_summary_toggled)
        header.addWidget(self.excel_summary_status, 1)
        header.addWidget(self.excel_summary_toggle)
        layout.addLayout(header)

        actions = QHBoxLayout()
        self.excel_summary_run_now_button = QPushButton("立即汇总")
        self.excel_summary_run_now_button.setObjectName("excelSummaryRunNowButton")
        self.excel_summary_run_now_button.setEnabled(False)
        self.excel_summary_run_now_button.clicked.connect(self._excel_summary_run_now)
        self.excel_summary_config_button = QPushButton("打开汇总配置")
        self.excel_summary_config_button.setObjectName("excelSummaryConfigButton")
        self.excel_summary_config_button.clicked.connect(self.open_monitor_config)
        actions.addWidget(self.excel_summary_run_now_button)
        actions.addWidget(self.excel_summary_config_button)
        actions.addStretch()
        layout.addLayout(actions)
        return group

    def _refresh_excel_summary_status(self):
        if self._excel_summary_status_worker is not None:
            return
        worker = TaskWorker(self.excel_summary_controller.status)
        self._excel_summary_status_worker = worker
        worker.signals.succeeded.connect(self._apply_excel_summary_status)
        worker.signals.failed.connect(
            lambda error: self._apply_excel_summary_status(
                {"running": False, "status": "error", "error": error}
            )
        )
        worker.signals.finished.connect(
            lambda worker=worker: self._excel_summary_status_finished(worker)
        )
        self.thread_pool.start(worker)

    def _excel_summary_status_finished(self, worker: TaskWorker):
        if self._excel_summary_status_worker is worker:
            self._excel_summary_status_worker = None

    def _apply_excel_summary_status(self, payload: dict):
        running = bool(payload.get("running"))
        status = str(payload.get("status", "stopped"))
        error = str(payload.get("error") or payload.get("last_error") or "")
        blocker = QSignalBlocker(self.excel_summary_toggle)
        self.excel_summary_toggle.setChecked(running)
        del blocker

        labels = {
            "starting": "启动中",
            "idle": "运行中",
            "waiting_for_devices": "等待设备资源",
            "summarizing": "汇总中",
            "stopping": "停止中",
            "stopped": "已停止",
            "config_error": "配置错误",
            "error": "状态异常",
        }
        text = labels.get(status, "运行中" if running else "已停止")
        next_run_at = str(payload.get("next_run_at") or "")
        if running and len(next_run_at) >= 16:
            text += f" · 下次 {next_run_at[5:16].replace('T', ' ')}"
        summary = payload.get("last_summary")
        if running and isinstance(summary, dict):
            finished_at = str(summary.get("finished_at") or "")
            row_count = int(summary.get("row_count", 0) or 0)
            if len(finished_at) >= 16:
                text += f" · 上次 {finished_at[5:16].replace('T', ' ')} · {row_count} 行"
        if error:
            text = f"{text}：{error}"
        self.excel_summary_status.setText(text)
        controls_idle = self._excel_summary_control_worker is None
        self.excel_summary_toggle.setEnabled(controls_idle)
        self.excel_summary_run_now_button.setEnabled(running and controls_idle)

    def _excel_summary_toggled(self, enabled: bool):
        if self._excel_summary_control_worker is not None:
            return
        action = "start" if enabled else "stop"
        callback = (
            self.excel_summary_controller.start
            if enabled
            else self.excel_summary_controller.stop
        )
        self._start_excel_summary_control(action, callback)

    def _excel_summary_run_now(self):
        if self._excel_summary_control_worker is not None:
            return
        self._start_excel_summary_control(
            "run_now", self.excel_summary_controller.run_now
        )

    def _start_excel_summary_control(
        self, action: str, callback: Callable[[], object]
    ):
        worker = TaskWorker(callback)
        self._excel_summary_control_worker = worker
        self.excel_summary_toggle.setEnabled(False)
        self.excel_summary_run_now_button.setEnabled(False)
        if action == "start":
            self.excel_summary_status.setText("启动中")
        elif action == "stop":
            self.excel_summary_status.setText("停止中")
        else:
            self.excel_summary_status.setText("已请求立即汇总")
        worker.signals.succeeded.connect(
            lambda result, action=action: self._excel_summary_control_succeeded(
                action, result
            )
        )
        worker.signals.failed.connect(
            lambda error, action=action: self._excel_summary_control_failed(
                action, error
            )
        )
        worker.signals.finished.connect(
            lambda worker=worker: self._excel_summary_control_finished(worker)
        )
        self.thread_pool.start(worker)

    def _excel_summary_control_succeeded(self, action: str, result: object):
        messages = {
            "start": "Excel 汇总进程已启动",
            "stop": "已请求停止 Excel 汇总进程",
            "run_now": "已请求立即执行 Excel 汇总",
        }
        self._append_log(messages[action])
        if isinstance(result, dict) and action == "start":
            self._apply_excel_summary_status(result)

    def _excel_summary_control_failed(self, action: str, error: str):
        names = {"start": "启动", "stop": "停止", "run_now": "立即汇总"}
        self.excel_summary_status.setText(f"{names[action]}失败：{error}")
        self._append_log(f"Excel 汇总{names[action]}失败：{error}")

    def _excel_summary_control_finished(self, worker: TaskWorker):
        if self._excel_summary_control_worker is worker:
            self._excel_summary_control_worker = None
            self.excel_summary_toggle.setEnabled(True)
            self.excel_summary_run_now_button.setEnabled(
                self.excel_summary_toggle.isChecked()
            )
        self._refresh_excel_summary_status()

    def _monitor_status_finished(self, worker: TaskWorker):
        if self._monitor_status_worker is worker:
            self._monitor_status_worker = None

    def _apply_monitor_status(self, payload: dict):
        running = bool(payload.get("running"))
        status = str(payload.get("status", "stopped"))
        error = str(payload.get("error") or payload.get("last_error") or "")
        blocker = QSignalBlocker(self.monitor_toggle)
        self.monitor_toggle.setChecked(running)
        del blocker

        labels = {
            "starting": "启动中",
            "idle": "运行中",
            "checking": "巡检中",
            "stopping": "停止中",
            "stopped": "已停止",
            "config_error": "配置错误",
            "error": "状态异常",
        }
        text = labels.get(status, "运行中" if running else "已停止")
        summary = payload.get("last_summary")
        if running and isinstance(summary, dict):
            checked_at = str(summary.get("checked_at", ""))
            checked_display = checked_at[5:16].replace("T", " ") if len(checked_at) >= 16 else ""
            alert_count = int(summary.get("active_alerts", 0) or 0)
            if checked_display:
                text = f"{text} · 上次 {checked_display} · 告警 {alert_count}"
        if error:
            text = f"{text}：{error}"
        self.monitor_status.setText(text)
        controls_idle = self._monitor_control_worker is None
        self.monitor_toggle.setEnabled(controls_idle)
        self.monitor_run_now_button.setEnabled(running and controls_idle)

    def _monitor_toggled(self, enabled: bool):
        if self._monitor_control_worker is not None:
            return
        action = "start" if enabled else "stop"
        callback = self.monitor_controller.start if enabled else self.monitor_controller.stop
        self._start_monitor_control(action, callback)

    def _monitor_run_now(self):
        if self._monitor_control_worker is not None:
            return
        self._start_monitor_control("run_now", self.monitor_controller.run_now)

    def _start_monitor_control(self, action: str, callback: Callable[[], object]):
        worker = TaskWorker(callback)
        self._monitor_control_worker = worker
        self.monitor_toggle.setEnabled(False)
        self.monitor_run_now_button.setEnabled(False)
        if action == "start":
            self.monitor_status.setText("启动中")
        elif action == "stop":
            self.monitor_status.setText("停止中")
        else:
            self.monitor_status.setText("已请求立即巡检")
        worker.signals.succeeded.connect(
            lambda result, action=action: self._monitor_control_succeeded(action, result)
        )
        worker.signals.failed.connect(
            lambda error, action=action: self._monitor_control_failed(action, error)
        )
        worker.signals.finished.connect(
            lambda worker=worker: self._monitor_control_finished(worker)
        )
        self.thread_pool.start(worker)

    def _monitor_control_succeeded(self, action: str, result: object):
        messages = {
            "start": "JSON 监控进程已启动",
            "stop": "已请求停止 JSON 监控进程",
            "run_now": "已请求立即执行 JSON 巡检",
        }
        self._append_log(messages[action])
        if isinstance(result, dict) and action == "start":
            self._apply_monitor_status(result)

    def _monitor_control_failed(self, action: str, error: str):
        names = {"start": "启动", "stop": "停止", "run_now": "立即巡检"}
        self.monitor_status.setText(f"{names[action]}失败：{error}")
        self._append_log(f"JSON 监控{names[action]}失败：{error}")

    def _monitor_control_finished(self, worker: TaskWorker):
        if self._monitor_control_worker is worker:
            self._monitor_control_worker = None
            self.monitor_toggle.setEnabled(True)
            self.monitor_run_now_button.setEnabled(self.monitor_toggle.isChecked())
        self._refresh_monitor_status()

    def _path_row(self, form: QFormLayout, label: str, directory: str, file_filter: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText("未选择")
        button = QPushButton("选择")
        button.setFixedWidth(58)
        button.clicked.connect(lambda: self._choose_file(edit, directory, file_filter))
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(edit, 1)
        row_layout.addWidget(button)
        form.addRow(label, row)
        return edit

    def _choose_file(self, edit: QLineEdit, directory: str, file_filter: str):
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", directory, file_filter)
        if path:
            edit.setText(path)

    def _populate_devices(self, checked_ids: set[str] | None = None):
        checked_ids = checked_ids or set()
        self.devices = sorted(self.devices, key=self._public_ip_sort_key)
        if hasattr(self, "public_ip_selector"):
            self._refresh_public_ip_options()
        self._refresh_public_ip_view_options()
        active_serials = {
            f"127.0.0.1:{mapping.get('local_port')}"
            for mapping in self._active_mappings()
            if mapping.get("local_port") is not None
        }
        self._updating_selection = True
        try:
            self.device_table.setRowCount(0)
            for device in self.devices:
                row = self.device_table.rowCount()
                self.device_table.insertRow(row)
                check = QTableWidgetItem()
                check.setCheckState(Qt.CheckState.Checked if device.id in checked_ids else Qt.CheckState.Unchecked)
                check.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.device_table.setItem(row, self.CHECK_COLUMN, check)
                sequence = QTableWidgetItem(str(row + 1))
                sequence.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                sequence.setFlags(sequence.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.device_table.setItem(row, self.SEQUENCE_COLUMN, sequence)
                app_name = device.app_name if device.app_name.strip() != device.package_name.strip() else ""
                app_item = QTableWidgetItem(app_name)
                connected = device.serial in active_serials
                app_item.setIcon(self._connection_status_icon(connected))
                app_item.setToolTip("ADB 已建立" if connected else "ADB 未建立")
                self.device_table.setItem(row, self.APP_NAME_COLUMN, app_item)
                self.device_table.setItem(row, self.GROUP_COLUMN, QTableWidgetItem(device.group))
                self.device_table.setItem(
                    row,
                    self.PUBLIC_IP_COLUMN,
                    QTableWidgetItem(device.public_ip),
                )
                self.device_table.setItem(row, self.REMOTE_ADDRESS_COLUMN, QTableWidgetItem(device.remote_address))
                self.device_table.setItem(row, self.LOCAL_PORT_COLUMN, QTableWidgetItem(str(device.local_port)))
                self.device_table.setItem(row, self.NOTE_COLUMN, QTableWidgetItem(device.note))
                self.device_table.setItem(row, self.PACKAGE_NAME_COLUMN, QTableWidgetItem(device.package_name))
                self.device_table.setItem(row, self.TAG_COLUMN, QTableWidgetItem(device.tag))
        finally:
            self._updating_selection = False
        self._refresh_column_filter_options()
        self._apply_column_filter()
        self.device_count.setText(f"{len(self.devices)} 条")
        self._update_dashboard_metrics()
        self._update_select_all_state()

    def _refresh_column_filter_options(self, *, reset: bool = False):
        """Refresh the value selector from the currently selected table column."""
        if not hasattr(self, "filter_column") or not hasattr(self, "filter_value"):
            return
        try:
            column = int(self.filter_column.currentData())
        except (TypeError, ValueError):
            return
        current = self.FILTER_ALL if reset else self.filter_value.currentData()
        if current is None:
            current = self.FILTER_ALL
        values = set()
        for row in range(self.device_table.rowCount()):
            item = self.device_table.item(row, column)
            if item is not None:
                values.add(item.text().strip())
        has_empty = "" in values
        values.discard("")
        header = self.TABLE_HEADERS[column]

        blocker = QSignalBlocker(self.filter_value)
        self.filter_value.clear()
        all_label = self.TAG_FILTER_ALL_LABEL if column == self.TAG_COLUMN else f"全部{header}"
        empty_label = self.TAG_FILTER_EMPTY_LABEL if column == self.TAG_COLUMN else f"空{header}"
        self.filter_value.addItem(all_label, self.FILTER_ALL)
        if has_empty:
            self.filter_value.addItem(empty_label, self.FILTER_EMPTY)
        for value in sorted(values, key=lambda text: (text.casefold(), text)):
            self.filter_value.addItem(value, value)
        index = self.filter_value.findData(current)
        self.filter_value.setCurrentIndex(index if index >= 0 else 0)
        del blocker

    def _filter_column_changed(self, _index: int | None = None):
        self._refresh_column_filter_options(reset=True)
        self._apply_column_filter()

    def _refresh_public_ip_view_options(self):
        if not hasattr(self, "public_ip_view_menu"):
            return
        options = public_ip_options(self.devices)
        if self._public_ip_view not in options:
            self._public_ip_view = None

        self.public_ip_view_menu.clear()
        entries: list[tuple[str, str | None]] = [("全部", None)]
        named_options = [public_ip for public_ip in PUBLIC_IP_PRESETS if public_ip in options]
        custom_options = [public_ip for public_ip in options if public_ip not in PUBLIC_IP_PRESETS]
        for public_ip in [*named_options, *custom_options]:
            server_name = self.PUBLIC_IP_SERVER_NAMES.get(public_ip)
            label = f"{server_name}（{public_ip}）" if server_name else public_ip
            entries.append((label, public_ip))
        for label, public_ip in entries:
            action = self.public_ip_view_menu.addAction(label)
            action.setCheckable(True)
            action.setData(public_ip)
            action.setChecked(public_ip == self._public_ip_view)
            action.triggered.connect(
                lambda _checked=False, value=public_ip: self._set_public_ip_view(value)
            )
        self._update_public_ip_view_button()

    def _set_public_ip_view(self, public_ip: str | None):
        self._public_ip_view = public_ip
        for action in self.public_ip_view_menu.actions():
            action.setChecked(action.data() == public_ip)
        self._update_public_ip_view_button()
        self._apply_column_filter()

    def _update_public_ip_view_button(self):
        label = (
            "全部"
            if self._public_ip_view is None
            else self.PUBLIC_IP_SERVER_NAMES.get(self._public_ip_view, self._public_ip_view)
        )
        self.public_ip_view_button.setText(f"公网 IP：{label}")

    def _apply_column_filter(self, _index: int | None = None):
        if not hasattr(self, "filter_column") or not hasattr(self, "filter_value"):
            return
        try:
            column = int(self.filter_column.currentData())
        except (TypeError, ValueError):
            return
        selected = self.filter_value.currentData()
        target = None if selected in (None, self.FILTER_ALL) else selected
        search_text = self.device_search.text().strip().casefold() if hasattr(self, "device_search") else ""
        searchable_columns = (
            self.APP_NAME_COLUMN,
            self.GROUP_COLUMN,
            self.PUBLIC_IP_COLUMN,
            self.REMOTE_ADDRESS_COLUMN,
            self.LOCAL_PORT_COLUMN,
            self.NOTE_COLUMN,
            self.PACKAGE_NAME_COLUMN,
            self.TAG_COLUMN,
        )
        visible_sequence = 0
        for row in range(self.device_table.rowCount()):
            item = self.device_table.item(row, column)
            value = item.text().strip() if item is not None else ""
            matches_column = target is None or (
                value == "" if target == self.FILTER_EMPTY else value == str(target).strip()
            )
            public_ip_item = self.device_table.item(row, self.PUBLIC_IP_COLUMN)
            public_ip = public_ip_item.text().strip() if public_ip_item is not None else ""
            matches_public_ip = (
                self._public_ip_view is None or public_ip == self._public_ip_view
            )
            search_haystack = " ".join(
                self.device_table.item(row, search_column).text().strip()
                for search_column in searchable_columns
                if self.device_table.item(row, search_column) is not None
            ).casefold()
            matches_search = not search_text or search_text in search_haystack
            matches = matches_column and matches_public_ip and matches_search
            self.device_table.setRowHidden(row, not matches)
            if matches:
                visible_sequence += 1
                sequence_item = self.device_table.item(row, self.SEQUENCE_COLUMN)
                if sequence_item is not None:
                    sequence_item.setText(str(visible_sequence))
        self._update_select_all_state()

    def _refresh_tag_filter_options(self):
        self._refresh_column_filter_options()

    def _apply_tag_filter(self, _index: int | None = None):
        self._apply_column_filter(_index)

    @staticmethod
    def _connection_status_icon(connected: bool) -> QIcon:
        pixmap = QPixmap(14, 14)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2c9a87" if connected else "#d14d4d"))
        painter.drawEllipse(1, 1, 12, 12)
        painter.end()
        return QIcon(pixmap)

    def _refresh_device_status_indicators(self):
        active_serials = {
            f"127.0.0.1:{mapping.get('local_port')}"
            for mapping in self._active_mappings()
            if mapping.get("local_port") is not None
        }
        blocker = QSignalBlocker(self.device_table)
        for row, device in enumerate(self.devices):
            item = self.device_table.item(row, self.APP_NAME_COLUMN)
            if item is None:
                continue
            connected = device.serial in active_serials
            item.setIcon(self._connection_status_icon(connected))
            item.setToolTip("ADB 已建立" if connected else "ADB 未建立")

        del blocker

    def _active_mappings(self) -> list[dict]:
        mappings = getattr(self.tunnel_manager, "active_mappings", ())
        try:
            return list(mappings)
        except TypeError:
            return []

    def _refresh_active_connections(self):
        mappings = sorted(
            self._active_mappings(),
            key=lambda mapping: int(mapping.get("local_port", 0)),
        )
        active_table = getattr(self, "active_connection_table", None)
        if active_table is not None:
            active_table.setRowCount(0)
            for mapping in mappings:
                row = active_table.rowCount()
                active_table.insertRow(row)
                serial = f"127.0.0.1:{mapping.get('local_port', '')}"
                device = next((item for item in self.devices if item.serial == serial), None)
                remote_address = (
                    f"{mapping.get('remote_ip', '')}:{mapping.get('remote_port', '')}"
                )
                public_ip = str(mapping.get("public_ip", "")).strip()
                if public_ip:
                    remote_address = f"{remote_address} via {public_ip}"
                values = [
                    device.label if device else serial,
                    serial,
                    remote_address,
                ]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    active_table.setItem(row, column, item)
            active_count = getattr(self, "active_connection_count", None)
            if active_count is not None:
                active_count.setText(f"{len(mappings)} 条")
        self._update_dashboard_metrics()
        self._refresh_device_status_indicators()

    def _device_table_item_changed(self, item: QTableWidgetItem):
        if item.column() == self.CHECK_COLUMN and not getattr(self, "_updating_selection", False):
            self._update_select_all_state()
        elif item.column() in self.FILTERABLE_COLUMNS and not getattr(self, "_updating_selection", False):
            if item.column() == self.filter_column.currentData():
                self._refresh_column_filter_options()
                self._apply_column_filter()

    def _select_all_state_changed(self, state: int):
        if getattr(self, "_updating_selection", False):
            return
        check_state = Qt.CheckState(state)
        if check_state == Qt.CheckState.PartiallyChecked:
            # A user click on a tri-state checkbox can enter the partial state;
            # treat that action as an explicit request to select every row.
            check_state = Qt.CheckState.Checked
        self._updating_selection = True
        try:
            for row in range(self.device_table.rowCount()):
                if self.device_table.isRowHidden(row):
                    continue
                item = self.device_table.item(row, self.CHECK_COLUMN)
                if item is not None:
                    item.setCheckState(check_state)
        finally:
            self._updating_selection = False
        self._update_select_all_state()

    def _update_select_all_state(self):
        if not hasattr(self, "select_all_devices"):
            return
        visible_rows = [
            row for row in range(self.device_table.rowCount()) if not self.device_table.isRowHidden(row)
        ]
        total = len(visible_rows)
        checked = sum(
            1
            for row in visible_rows
            if (item := self.device_table.item(row, self.CHECK_COLUMN)) is not None
            and item.checkState() == Qt.CheckState.Checked
        )
        if total == 0 or checked == 0:
            state = Qt.CheckState.Unchecked
        elif checked == total:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        blocker = QSignalBlocker(self.select_all_devices)
        self.select_all_devices.setCheckState(state)
        self.select_all_devices.setEnabled(total > 0)
        del blocker

    def _restore_connection_value(self, key: str, default: str) -> str:
        value = self._settings.value(key, default)
        return str(value) if value is not None else default

    def _save_connection_values(self):
        self._settings.setValue(
            "connection/public_ip",
            self.public_ip_selector.currentText().strip(),
        )
        self._settings.setValue("connection/start_local_port", self.start_local_port.text().strip())
        self._settings.setValue("connection/loop_count", self.loop_count.text().strip())
        self._settings.setValue("connection/start_ip_first", self.start_ip_first.text().strip())
        self._settings.sync()

    def _save_lua_path(self):
        self._settings.setValue("paths/lua_path", self.lua_path.text().strip())
        self._settings.sync()

    def _apply_saved_connection_values(self):
        blockers = [
            QSignalBlocker(self.public_ip_selector),
            QSignalBlocker(self.start_local_port),
            QSignalBlocker(self.loop_count),
            QSignalBlocker(self.start_ip_first),
        ]
        saved_public_ip = self._restore_connection_value(
            "connection/public_ip",
            DEFAULT_PUBLIC_IP,
        )
        self._refresh_public_ip_options(saved_public_ip)
        self.start_local_port.setText(self._restore_connection_value("connection/start_local_port", "63000"))
        self.loop_count.setText(self._restore_connection_value("connection/loop_count", "2"))
        self.start_ip_first.setText(self._restore_connection_value("connection/start_ip_first", "2"))
        del blockers

    def _refresh_public_ip_options(self, preferred: str | None = None):
        current = (preferred or self.public_ip_selector.currentText()).strip()
        options = public_ip_options(self.devices)
        blocker = QSignalBlocker(self.public_ip_selector)
        self.public_ip_selector.clear()
        self.public_ip_selector.addItems(options)
        if current:
            self.public_ip_selector.setCurrentText(current)
        elif options:
            self.public_ip_selector.setCurrentIndex(0)
        del blocker

    def _apply_saved_lua_path(self):
        blocker = QSignalBlocker(self.lua_path)
        saved_path = self._restore_connection_value("paths/lua_path", "").strip()
        self.lua_path.setText(saved_path or str(DEFAULT_LUA_PATH))
        del blocker

    def _toggle_connection_panel(self, expanded: bool):
        self.connection_panel.setVisible(expanded)
        self.connection_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def _selected_devices(self) -> list[Device]:
        selected = []
        for row, device in enumerate(self.devices):
            if self.device_table.isRowHidden(row):
                continue
            item = self.device_table.item(row, self.CHECK_COLUMN)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected.append(device)
        return selected

    def _resolve_operation_devices(self, devices: list[Device] | None) -> list[Device] | None:
        if devices is not None:
            return list(devices)
        return self._require_selection()

    def _build_single_apk_task(self, device: Device, urls: list[str]) -> tuple[Device, str]:
        if len(urls) == 1:
            return device, urls[0]
        if len(urls) != len(self.devices):
            raise ValueError("OSS URL count must be 1 or match the device queue")
        try:
            row = self.devices.index(device)
        except ValueError as exc:
            raise ValueError("The requested device is not in the current queue") from exc
        return device, urls[row]

    def _append_log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.task_log.appendPlainText(f"[{timestamp}] {message}")

    def _report_scrcpy_status(self, device: Device, status: str, detail: str = ""):
        self.scrcpy_status_reported.emit(
            {"device_id": device.id, "status": status, "detail": detail}
        )

    def _scrcpy_status_updated(self, payload: dict):
        device_id = str(payload.get("device_id", ""))
        status = str(payload.get("status", ""))
        detail = str(payload.get("detail", ""))
        if not device_id or not status:
            return
        self._scrcpy_statuses[device_id] = status
        message = f"{device_id}: {status}"
        if detail:
            message += f" ({detail})"
        self._append_log(message)

    def _require_selection(self) -> list[Device] | None:
        devices = self._selected_devices()
        if not devices:
            QMessageBox.information(self, "未选择记录", "请先勾选连接队列记录。")
            return None
        return devices

    def _create_task_group(
        self,
        name: str,
        total: int,
        *,
        metadata: dict[str, Any] | None = None,
        on_finished: Callable[[_UiTaskGroup], None] | None = None,
    ) -> int:
        if total <= 0:
            raise ValueError("task group total must be positive")
        group_id = self._next_task_group_id
        self._next_task_group_id += 1
        self._task_groups[group_id] = _UiTaskGroup(
            group_id=group_id,
            name=str(name),
            total=int(total),
            metadata=dict(metadata or {}),
            on_finished=on_finished,
        )
        self.thread_pool.setMaxThreadCount(self.concurrency.value())
        self._active_task_name = str(name)
        self._stop_requested = False
        self._refresh_task_ui()
        return group_id

    def _enqueue_task_job(
        self,
        group_id: int,
        resources: Iterable[str],
        callback: Callable[[], object],
        *,
        process_spec: ProcessTaskSpec | None = None,
        on_succeeded: Callable[[object], None] | None = None,
        on_failed: Callable[[str], None] | None = None,
    ) -> int:
        if group_id not in self._task_groups:
            raise KeyError(f"unknown task group: {group_id}")
        job_id = self._next_task_job_id
        self._next_task_job_id += 1
        self._job_groups[job_id] = group_id

        def start_job() -> None:
            self._start_task_job(
                job_id,
                callback,
                process_spec=process_spec,
                on_succeeded=on_succeeded,
                on_failed=on_failed,
            )

        try:
            scheduler_task_id = self._task_scheduler.submit(resources, start_job)
        except Exception:
            self._job_groups.pop(job_id, None)
            raise
        self._job_scheduler_tasks[job_id] = scheduler_task_id
        self._scheduler_jobs[scheduler_task_id] = job_id
        return job_id

    def _start_task_job(
        self,
        job_id: int,
        callback: Callable[[], object],
        *,
        process_spec: ProcessTaskSpec | None,
        on_succeeded: Callable[[object], None] | None,
        on_failed: Callable[[str], None] | None,
    ) -> None:
        worker = TaskWorker(callback, process_spec=process_spec)
        self._job_workers[job_id] = worker
        self._worker_jobs[worker] = job_id
        self._register_worker(worker)
        worker.signals.succeeded.connect(
            lambda result, job_id=job_id, callback=on_succeeded: self._task_job_succeeded(
                job_id, result, callback
            )
        )
        worker.signals.failed.connect(
            lambda error, job_id=job_id, callback=on_failed: self._task_job_failed(
                job_id, error, callback
            )
        )
        worker.signals.finished.connect(
            lambda worker=worker, job_id=job_id: self._task_job_finished(job_id, worker)
        )
        self.thread_pool.start(worker)

    def _task_job_succeeded(
        self,
        job_id: int,
        result: object,
        callback: Callable[[object], None] | None,
    ) -> None:
        if callback is None:
            return
        try:
            callback(result)
        except Exception as exc:
            group = self._task_groups.get(self._job_groups.get(job_id, -1))
            if group is not None:
                group.failed += 1
            self._append_log(f"任务结果处理失败：{exc}")

    def _task_job_failed(
        self,
        job_id: int,
        error: str,
        callback: Callable[[str], None] | None,
    ) -> None:
        group = self._task_groups.get(self._job_groups.get(job_id, -1))
        if group is not None:
            group.failed += 1
        if callback is None:
            self._append_log(f"任务失败：{error}")
            return
        try:
            callback(error)
        except Exception as exc:
            self._append_log(f"任务错误处理失败：{exc}")

    def _task_job_finished(self, job_id: int, worker: TaskWorker) -> None:
        self._unregister_worker(worker)
        self._worker_jobs.pop(worker, None)
        self._job_workers.pop(job_id, None)
        scheduler_task_id = self._job_scheduler_tasks.pop(job_id, None)
        if scheduler_task_id is not None:
            self._scheduler_jobs.pop(scheduler_task_id, None)
            self._task_scheduler.complete(scheduler_task_id)

        group_id = self._job_groups.pop(job_id, None)
        group = self._task_groups.get(group_id) if group_id is not None else None
        if group is None:
            self._refresh_task_ui()
            return
        group.completed += 1
        if worker.cancel_requested:
            group.cancelled += 1
        if group.completed >= group.total:
            self._finish_task_group(group.group_id)
        else:
            self._refresh_task_ui()

    def _finish_task_group(self, group_id: int) -> None:
        group = self._task_groups.pop(group_id, None)
        if group is None:
            return
        callback = group.on_finished
        self._refresh_task_ui(last_group=group)
        if callback is not None:
            try:
                callback(group)
            except Exception as exc:
                self._append_log(f"{group.name}收尾失败：{exc}")

    def _refresh_task_ui(self, *, last_group: _UiTaskGroup | None = None) -> None:
        active_groups = list(self._task_groups.values())
        busy = bool(active_groups)
        edit_controls = [
            self.add_record_button,
            self.save_record_button,
            self.delete_record_button,
            self.reload_button,
            self.generate_queue_button,
            self.renumber_ports_button,
        ]
        for control in edit_controls:
            control.setEnabled(not busy)
        self.stop_task_button.setEnabled(busy)

        if busy:
            total = sum(group.total for group in active_groups)
            completed = sum(group.completed for group in active_groups)
            self._task_total = total
            self._task_finished = completed
            self.task_progress.setRange(0, total)
            self.task_progress.setValue(completed)
            self.task_status.setText(
                f"运行中 {len(active_groups)} 个任务组：{completed}/{total}"
            )
        elif last_group is not None:
            self._task_total = last_group.total
            self._task_finished = last_group.completed
            self.task_progress.setRange(0, last_group.total)
            self.task_progress.setValue(last_group.completed)
            if last_group.cancelled:
                suffix = "已终止"
            elif last_group.failed:
                suffix = "失败"
            else:
                suffix = "完成"
            self.task_status.setText(f"{last_group.name}：{suffix}")
            self._append_log(
                f"{last_group.name}{suffix}（完成 {last_group.completed}/{last_group.total}，"
                f"失败 {last_group.failed}，取消 {last_group.cancelled}）"
            )
            self._stop_requested = False
        else:
            self.stop_task_button.setEnabled(False)
        self._update_header_task()

    def stop_current_task(self):
        """Stop queued work and prevent workers that have not started from running."""
        if not self.stop_task_button.isEnabled():
            return
        if self._task_groups:
            self._stop_requested = True
            cancelled_scheduler_tasks = self._task_scheduler.cancel_pending()
            groups_to_finish: set[int] = set()
            for scheduler_task_id in cancelled_scheduler_tasks:
                job_id = self._scheduler_jobs.pop(scheduler_task_id, None)
                if job_id is None:
                    continue
                self._job_scheduler_tasks.pop(job_id, None)
                group_id = self._job_groups.pop(job_id, None)
                group = self._task_groups.get(group_id) if group_id is not None else None
                if group is None:
                    continue
                group.completed += 1
                group.cancelled += 1
                if group.completed >= group.total:
                    groups_to_finish.add(group.group_id)

            self._apk_cancelled += len(cancelled_scheduler_tasks)
            self._apk_pending.clear()
            self._scrcpy_queue.clear()
            self._adb_shell_queue.clear()
            self._lua_queue.clear()
            self._pull_queue.clear()
            for worker in tuple(self._workers):
                request_cancel = getattr(worker, "request_cancel", None)
                if callable(request_cancel):
                    request_cancel()
            for pid in tuple(self._task_processes.values()):
                terminate_process_tree(pid)
            self._task_processes.clear()
            self._cleanup_task_resources()
            cleanup_json_pull_staging(JSON_OUTPUT_DIR)
            self._append_log("已请求终止全部运行中和排队中的任务")
            for group_id in groups_to_finish:
                self._finish_task_group(group_id)
            if self._task_groups:
                self._refresh_task_ui()
                self.stop_task_button.setEnabled(False)
            return
        self._stop_requested = True
        self._apk_cancelled += len(self._apk_pending)
        self._apk_pending.clear()
        self._scrcpy_queue.clear()
        self._adb_shell_queue.clear()
        self._lua_queue.clear()
        self._pull_queue.clear()
        for worker in tuple(self._workers):
            request_cancel = getattr(worker, "request_cancel", None)
            if callable(request_cancel):
                request_cancel()
        for pid in tuple(self._task_processes.values()):
            terminate_process_tree(pid)
        self._task_processes.clear()
        self._cleanup_task_resources()
        if self._pull_queue_artifact == JSON_ARTIFACT:
            cleanup_json_pull_staging(JSON_OUTPUT_DIR)
        self.stop_task_button.setEnabled(False)
        self._append_log(f"已请求终止当前任务：{self._active_task_name}")
        if not self._workers:
            if self._pull_queue_artifact:
                self._finish_pull_queue()
            elif self._lua_queue_path:
                self._finish_lua_queue()
            else:
                self._set_apk_controls_enabled(True)

    def _register_worker(self, worker: TaskWorker) -> TaskWorker:
        """Track a worker and any child PID it owns for later cancellation."""
        self._workers.add(worker)
        worker.signals.process_started.connect(
            lambda pid, worker=worker: self._register_worker_pid(worker, pid)
        )
        worker.signals.finished.connect(
            lambda worker=worker: self._task_processes.pop(worker, None)
        )
        return worker

    def _register_worker_pid(self, worker: TaskWorker, pid: int) -> None:
        process_id = int(pid)
        if self._stop_requested or worker.cancel_requested:
            terminate_process_tree(process_id)
            return
        self._task_processes[worker] = process_id

    def _unregister_worker(self, worker: TaskWorker) -> None:
        self._workers.discard(worker)
        self._task_processes.pop(worker, None)

    def _cleanup_task_resources(self) -> None:
        """Release resources owned by the current task, tolerating partial startup."""
        cleanup = getattr(self.operations, "cleanup_owned_processes", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception as exc:
                self._append_log(f"Task resource cleanup failed: {exc}")
        disconnect = getattr(self.tunnel_manager, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception as exc:
                self._append_log(f"ADB/SSH cleanup failed: {exc}")

    def _device_process_factory(
        self,
        operation: str,
        *args: object,
        **kwargs: object,
    ) -> Callable[[Device], ProcessTaskSpec] | None:
        """Use child processes only with the real spawn-safe service backend."""
        if not self._can_use_device_processes():
            return None

        def factory(device: Device) -> ProcessTaskSpec:
            return process_spec_for_device_operation(operation, device, *args, **kwargs)

        return factory

    def _can_use_device_processes(self) -> bool:
        return (
            isinstance(self.operations, DeviceOperations)
            and self.operations.runner is subprocess.run
            and self.operations.popen is subprocess.Popen
            and self.operations.downloader is urlretrieve
            and self.operations.adb_path == ADB_PATH
            and self.operations.scrcpy_path == SCRCPY_PATH
        )

    def _dispatch(
        self,
        task_name: str,
        callback: Callable[[Device], object],
        devices: list[Device] | None = None,
        *,
        process_spec_factory: Callable[[Device], ProcessTaskSpec] | None = None,
    ):
        devices = devices or self._require_selection()
        if not devices:
            return
        self.thread_pool.setMaxThreadCount(self.concurrency.value())
        self._append_log(f"开始{task_name}，目标记录 {len(devices)} 条")
        group_id = self._create_task_group(task_name, len(devices))
        for device in devices:
            process_spec = process_spec_factory(device) if process_spec_factory else None
            self._enqueue_task_job(
                group_id,
                [device_resource_key(device)],
                lambda device=device: callback(device),
                process_spec=process_spec,
                on_succeeded=lambda result, device=device, task_name=task_name: self._operation_succeeded(
                    device, result, task_name
                ),
                on_failed=lambda error, device=device: self._operation_failed(device, error),
            )

    def _operation_succeeded(
        self,
        device: Device,
        result: object,
        task_name: str | None = None,
    ):
        if result is None and (task_name or self._active_task_name) == "运行 Airtest":
            self._append_log(f"{device.serial}: Airtest completed")
            return
        self._append_log(f"{device.serial}：{result}")

    def _operation_failed(self, device: Device, error: str):
        self._append_log(f"{device.serial}：失败，{error}")

    def _pull_operation_succeeded(
        self,
        device: Device,
        result: object,
        artifact_type: str,
        destination: Path,
    ):
        if artifact_type == JSON_ARTIFACT:
            count = len(result) if isinstance(result, (list, tuple)) else 0
            self._append_log(
                f"{device.serial}: JSON 拉取完成，{count} 个文件 -> {destination}"
            )
            return
        self._operation_succeeded(device, result)

    def _pull_operation_failed(
        self,
        device: Device,
        error: str,
        artifact_type: str,
        destination: Path,
    ):
        self._pull_queue_failed = True
        self._append_log(
            f"{device.serial}: {artifact_type} 拉取失败，目标 {destination}，{error}"
        )

    def _worker_finished(self, worker: TaskWorker):
        self._unregister_worker(worker)
        self._task_finished += 1
        self.task_progress.setValue(self._task_finished)
        if self._task_finished >= self._task_total:
            stopped = self._stop_requested
            if stopped:
                self._cleanup_task_resources()
            self.task_status.setText(f"{self._active_task_name}：已终止" if stopped else f"{self._active_task_name}：完成")
            self._append_log(f"{self._active_task_name}{'已终止' if stopped else '已完成'}")
            for button in self._operation_buttons + [self.add_record_button, self.save_record_button, self.delete_record_button]:
                button.setEnabled(True)
            self.reload_button.setEnabled(True)
            self.stop_task_button.setEnabled(False)
            self._stop_requested = False

    def _read_oss_urls(self) -> list[str]:
        if not OSS_URL_FILE.is_file():
            raise FileNotFoundError(f"OSS URL 文件不存在：{OSS_URL_FILE}")
        with OSS_URL_FILE.open("r", encoding="utf-8-sig") as handle:
            urls = [line.strip() for line in handle if line.strip()]
        if not urls:
            raise ValueError("OSS URL 文件中没有可用下载链接")
        return urls

    @staticmethod
    def _build_apk_tasks(
        devices: list[Device],
        urls: list[str],
        *,
        reuse_single_url: bool = False,
    ) -> list[tuple[Device, str]]:
        if reuse_single_url and len(urls) == 1:
            return [(device, urls[0]) for device in devices]
        if len(devices) != len(urls):
            raise ValueError(f"选中设备数量 ({len(devices)}) 与下载链接数量 ({len(urls)}) 不一致")
        return list(zip(devices, urls))

    def _set_apk_controls_enabled(self, enabled: bool):
        controls = self._operation_buttons + [
            self.add_record_button,
            self.save_record_button,
            self.delete_record_button,
            self.connect_button,
            self.disconnect_button,
            self.generate_queue_button,
        ]
        for button in controls:
            button.setEnabled(enabled)
        self.reload_button.setEnabled(enabled)
        self.device_table.setEnabled(enabled)
        self.stop_task_button.setEnabled(not enabled)

    def _start_apk_install_queue(self, tasks: list[tuple[Device, str]]):
        if not tasks:
            return
        self._apk_pending = deque(tasks)
        self._apk_worker_tasks = {}
        self._apk_active = 0
        self._apk_completed = 0
        self._apk_cancelled = 0
        self._apk_successes = []
        self._apk_failures = []
        self._apk_selected_ids = {device.id for device, _ in tasks}
        self.thread_pool.setMaxThreadCount(self.concurrency.value())
        self._append_log(f"开始下载安装 {len(tasks)} 个任务，并发数 {self.concurrency.value()}")
        group_id = self._create_task_group(
            "下载安装 APK",
            len(tasks),
            metadata={
                "successes": [],
                "failures": [],
                "selected_ids": {device.id for device, _ in tasks},
            },
            on_finished=self._apk_group_finished,
        )
        for task in tasks:
            device, url = task
            process_spec = None
            if self._can_use_device_processes():
                process_spec = process_spec_for_device_operation(
                    "install_apk_from_url",
                    device,
                    url,
                )
            self._enqueue_task_job(
                group_id,
                [device_resource_key(device)],
                lambda device=device, url=url: self.operations.install_apk_from_url(device, url),
                process_spec=process_spec,
                on_succeeded=lambda result, group_id=group_id, task=task: self._apk_group_task_succeeded(
                    group_id, task, result
                ),
                on_failed=lambda error, group_id=group_id, task=task: self._apk_group_task_failed(
                    group_id, task, error, count_failure=False
                ),
            )

    def _apk_group_task_succeeded(
        self,
        group_id: int,
        task: tuple[Device, str],
        result: object,
    ) -> None:
        group = self._task_groups.get(group_id)
        if group is None:
            return
        device, url = task
        if not isinstance(result, dict):
            self._apk_group_task_failed(
                group_id,
                task,
                "下载安装结果格式无效",
                count_failure=True,
            )
            return
        app_name = str(result.get("app_name", "")).strip()
        package_name = str(result.get("package_name", "")).strip()
        if not app_name or not package_name:
            self._apk_group_task_failed(
                group_id,
                task,
                "下载安装结果缺少应用名或包名",
                count_failure=True,
            )
            return
        updated_devices = [
            replace(item, app_name=app_name, package_name=package_name)
            if item.id == device.id
            else item
            for item in self.devices
        ]
        try:
            if not self._persist_devices(
                updated_devices,
                checked_ids=set(group.metadata["selected_ids"]),
            ):
                self._apk_group_task_failed(
                    group_id,
                    task,
                    "Configuration save was rejected",
                    count_failure=True,
                )
                return
        except (OSError, ValueError) as exc:
            self._apk_group_task_failed(
                group_id,
                task,
                f"结果保存失败：{exc}",
                count_failure=True,
            )
            return
        group.metadata["successes"].append(
            {
                "device_id": device.id,
                "app_name": app_name,
                "package_name": package_name,
                "url": url,
            }
        )
        self._append_log(
            f"{device.label}：安装完成，应用名 {app_name}，包名 {package_name}"
        )

    def _apk_group_task_failed(
        self,
        group_id: int,
        task: tuple[Device, str],
        error: str,
        *,
        count_failure: bool,
    ) -> None:
        group = self._task_groups.get(group_id)
        if group is None:
            return
        device, url = task
        group.metadata["failures"].append(
            {"device_id": device.id, "url": url, "error": str(error)}
        )
        if count_failure:
            group.failed += 1
        self._append_log(f"{device.label}：下载安装失败，{error}")

    def _apk_group_finished(self, group: _UiTaskGroup) -> None:
        successes = list(group.metadata.get("successes", []))
        failures = list(group.metadata.get("failures", []))
        self._apk_successes = successes
        self._apk_failures = failures
        self._apk_completed = group.completed
        self._apk_cancelled = group.cancelled
        self._apk_pending.clear()
        self._apk_active = 0
        self._append_log(
            f"下载安装 APK 清单：成功 {len(successes)}，失败 {len(failures)}，取消 {group.cancelled}"
        )
        for failure in failures:
            self._append_log(f"失败任务 {failure['device_id']}：{failure['error']}")

    def _fill_apk_workers(self):
        while self._apk_pending and not self._stop_requested and self._apk_active < self.concurrency.value():
            task = self._apk_pending.popleft()
            self._apk_active += 1
            self._start_apk_worker(task)

    def _start_apk_worker(self, task: tuple[Device, str]):
        device, url = task
        process_spec = None
        if self._can_use_device_processes():
            process_spec = process_spec_for_device_operation(
                "install_apk_from_url",
                device,
                url,
            )
        worker = TaskWorker(
            lambda device=device, url=url: self.operations.install_apk_from_url(device, url),
            process_spec=process_spec,
        )
        self._apk_worker_tasks[worker] = task
        self._register_worker(worker)
        worker.signals.succeeded.connect(lambda result, task=task: self._apk_task_succeeded(task, result))
        worker.signals.failed.connect(lambda error, task=task: self._apk_task_failed(task, error))
        worker.signals.finished.connect(lambda worker=worker: self._apk_task_finished(worker))
        self.thread_pool.start(worker)

    def _apk_task_succeeded(self, task: tuple[Device, str], result: object):
        device, url = task
        if not isinstance(result, dict):
            self._apk_task_failed(task, "下载安装结果格式无效")
            return
        app_name = str(result.get("app_name", "")).strip()
        package_name = str(result.get("package_name", "")).strip()
        if not app_name or not package_name:
            self._apk_task_failed(task, "下载安装结果缺少应用名或包名")
            return

        updated_devices = [
            replace(device_row, app_name=app_name, package_name=package_name)
            if device_row.id == device.id
            else device_row
            for device_row in self.devices
        ]
        try:
            if not self._persist_devices(updated_devices, checked_ids=self._apk_selected_ids):
                self._apk_task_failed(task, "Configuration save was rejected")
                return
        except (OSError, ValueError) as exc:
            self._apk_task_failed(task, f"结果保存失败：{exc}")
            return

        success = {
            "device_id": device.id,
            "app_name": app_name,
            "package_name": package_name,
            "url": url,
        }
        self._apk_successes.append(success)
        self._append_log(f"{device.label}：安装完成，应用名 {app_name}，包名 {package_name}")

    def _apk_task_failed(self, task: tuple[Device, str], error: str):
        device, url = task
        failure = {"device_id": device.id, "url": url, "error": str(error)}
        self._apk_failures.append(failure)
        self._append_log(f"{device.label}：下载安装失败，{error}")

    def _apk_task_finished(self, worker: TaskWorker):
        self._unregister_worker(worker)
        self._apk_worker_tasks.pop(worker, None)
        self._apk_active = max(0, self._apk_active - 1)
        self._apk_completed += 1
        self._task_finished = self._apk_completed
        self.task_progress.setValue(self._apk_completed)
        if not self._stop_requested:
            self._fill_apk_workers()
        if self._apk_active or (not self._stop_requested and self._apk_completed < self._task_total):
            return

        success_count = len(self._apk_successes)
        failure_count = len(self._apk_failures)
        if self._stop_requested:
            self._cleanup_task_resources()
            self.task_status.setText(
                f"{self._active_task_name}：已终止（完成 {self._apk_completed}，取消 {self._apk_cancelled}）"
            )
            self._append_log(
                f"{self._active_task_name}已终止：完成 {self._apk_completed}，取消 {self._apk_cancelled}，"
                f"成功 {success_count}，失败 {failure_count}"
            )
        else:
            self.task_status.setText(
                f"{self._active_task_name}：完成（成功 {success_count}，失败 {failure_count}）"
            )
            self._append_log(f"{self._active_task_name}完成清单：成功 {success_count}，失败 {failure_count}")
        for failure in self._apk_failures:
            self._append_log(f"失败任务 {failure['device_id']}：{failure['error']}")
        self._set_apk_controls_enabled(True)
        self._stop_requested = False

    def _read_table_devices(self) -> list[Device]:
        updated = []
        local_ports = set()
        remote_addresses = set()
        for row, existing in enumerate(self.devices):
            app_name = self.device_table.item(row, self.APP_NAME_COLUMN).text().strip()
            public_ip = normalize_public_ip(
                self.device_table.item(row, self.PUBLIC_IP_COLUMN).text(),
                row_number=row + 1,
            )
            remote_address = self.device_table.item(row, self.REMOTE_ADDRESS_COLUMN).text().strip()
            local_port_text = self.device_table.item(row, self.LOCAL_PORT_COLUMN).text().strip()
            note = self.device_table.item(row, self.NOTE_COLUMN).text().strip()
            package_name = self.device_table.item(row, self.PACKAGE_NAME_COLUMN).text().strip()
            tag = self.device_table.item(row, self.TAG_COLUMN).text().strip()
            group = self.device_table.item(row, self.GROUP_COLUMN).text().strip()
            try:
                remote_ip, remote_port_text = remote_address.rsplit(":", 1)
                ipaddress.ip_address(remote_ip)
                remote_port = int(remote_port_text)
                local_port = int(local_port_text)
            except (ValueError, IndexError) as exc:
                raise ValueError(f"第 {row + 1} 条记录的地址或端口格式错误") from exc
            if not 1 <= remote_port <= 65535 or not 1 <= local_port <= 65535:
                raise ValueError(f"第 {row + 1} 条记录的端口超出范围")
            if local_port in local_ports:
                raise ValueError(f"本地端口重复：{local_port}")
            remote_key = (public_ip, remote_ip, remote_port)
            if remote_key in remote_addresses:
                raise ValueError(f"远端地址重复：{remote_address}")
            local_ports.add(local_port)
            remote_addresses.add(remote_key)
            updated.append(
                replace(
                    existing,
                    sort_order=(row + 1) * 10,
                    app_name=app_name,
                    public_ip=public_ip,
                    remote_ip=remote_ip,
                    remote_port=remote_port,
                    local_port=local_port,
                    note=note,
                    package_name=package_name,
                    tag=tag,
                    group=group,
                )
            )
        return updated

    def save_queue(self):
        try:
            updated = self._read_table_devices()
            if not self._persist_devices(updated):
                return
            self._refresh_active_connections()
            self._append_log(f"已保存 {len(self.devices)} 条连接记录")
        except (OSError, ValueError) as exc:
            self._append_log(f"保存失败：{exc}")
            QMessageBox.warning(self, "保存失败", str(exc))

    def renumber_local_ports(self):
        """Persist sequential ports, requiring an explicit disconnect beforehand."""
        checked_ids = {device.id for device in self._selected_devices()}
        try:
            start_port_text = self.start_local_port.text().strip()
            if not start_port_text:
                raise ValueError("端口重排的起始端口不能为空")
            try:
                start_port = int(start_port_text)
            except ValueError as exc:
                raise ValueError("端口重排的起始端口必须是单个整数") from exc
            if self._active_mappings():
                raise ValueError("当前仍有 ADB 连接，请先点击“断开连接”")
            current_devices = self._read_table_devices()
            updated_devices = renumber_device_ports(current_devices, start_port=start_port)
            if not self._persist_devices(updated_devices, checked_ids=checked_ids):
                return
            self._refresh_active_connections()
            self._append_log(f"已将 {len(self.devices)} 条记录的本地端口从 {start_port} 开始重排")
        except (OSError, ValueError) as exc:
            self._append_log(f"端口重排失败：{exc}")
            QMessageBox.warning(self, "端口重排失败", str(exc))

    def add_record(self):
        try:
            public_ip = normalize_public_ip(self.public_ip_selector.currentText())
        except ValueError as exc:
            QMessageBox.warning(self, "\u516c\u7f51 IP \u65e0\u6548", str(exc))
            return
        local_ports = {device.local_port for device in self.devices}
        local_port = 63000
        while local_port in local_ports:
            local_port += 1
        record = Device(
            id=next_device_id(self.devices),
            enabled=True,
            sort_order=(len(self.devices) + 1) * 10,
            app_name="",
            remote_ip="10.237.0.1",
            remote_port=5555,
            local_port=local_port,
            note="",
            package_name="",
            public_ip=public_ip,
        )
        self.devices.append(record)
        self._populate_devices({record.id})
        self._append_log("已新增一条记录，请编辑后保存")

    def delete_records(self):
        selected = self._selected_devices()
        if not selected:
            QMessageBox.information(self, "未选择记录", "请先勾选要删除的连接记录。")
            return
        active_serials = {f"127.0.0.1:{mapping['local_port']}" for mapping in self.tunnel_manager.active_mappings}
        if any(device.serial in active_serials for device in selected):
            QMessageBox.warning(self, "记录正在连接", "请先断开连接，再删除记录。")
            return
        answer = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除选中的 {len(selected)} 条连接记录吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        selected_ids = {device.id for device in selected}
        try:
            updated_devices = self._read_table_devices()
            remaining_devices = [device for device in updated_devices if device.id not in selected_ids]
            if not self._persist_devices(remaining_devices):
                return
            self._append_log(f"已删除 {len(selected)} 条连接记录")
        except (OSError, ValueError) as exc:
            self._append_log(f"删除失败：{exc}")
            QMessageBox.warning(self, "删除失败", str(exc))

    def generate_queue(self):
        try:
            public_ip = normalize_public_ip(self.public_ip_selector.currentText())
            batches = build_connection_batches(
                self.start_local_port.text(), self.loop_count.text(), self.start_ip_first.text()
            )
        except ValueError as exc:
            self._append_log(f"连接参数无效：{exc}")
            QMessageBox.warning(self, "连接参数错误", str(exc))
            return

        existing_local = {device.local_port for device in self.devices}
        existing_remote = {
            (device.public_ip, device.remote_ip, device.remote_port)
            for device in self.devices
        }
        generated = []
        for batch in batches:
            added_count = 0
            next_local_port = batch["start_local_port"]
            next_ip_last_octet = batch["start_ip_first"]
            while added_count < batch["loop_count"]:
                while next_local_port <= 65535 and next_local_port in existing_local:
                    next_local_port += 1
                while next_ip_last_octet <= 254 and (
                    public_ip,
                    f"10.237.0.{next_ip_last_octet}",
                    5555,
                ) in existing_remote:
                    next_ip_last_octet += 1
                if next_local_port > 65535 or next_ip_last_octet > 254:
                    message = f"无法为当前批次新增 {batch['loop_count']} 条记录：本地端口或远端 IP 已超出范围"
                    self._append_log(message)
                    QMessageBox.warning(self, "生成队列失败", message)
                    return
                local_port = next_local_port
                remote_ip = f"10.237.0.{next_ip_last_octet}"
                remote_key = (public_ip, remote_ip, 5555)
                generated.append(
                    Device(
                        id="",
                        enabled=True,
                        sort_order=0,
                        app_name="",
                        remote_ip=remote_ip,
                        remote_port=5555,
                        local_port=local_port,
                        note="",
                        package_name="",
                        public_ip=public_ip,
                    )
                )
                existing_local.add(local_port)
                existing_remote.add(remote_key)
                added_count += 1
                next_local_port += 1
                next_ip_last_octet += 1
        if not generated:
            self._append_log("没有生成新记录，可能与现有端口或远端地址重复")
            return
        for record in generated:
            record_index = len(self.devices) + 1
            self.devices.append(replace(record, id=next_device_id(self.devices), sort_order=record_index * 10))
        generated_ids = {record.id for record in self.devices[-len(generated):]}
        if not self._persist_devices(self.devices, checked_ids=generated_ids):
            return
        self._append_log(f"已生成并保存 {len(generated)} 条连接记录")

    def install_apk(self, *, devices: list[Device] | None = None):
        explicit_devices = devices is not None
        devices = self._resolve_operation_devices(devices)
        if not devices:
            return
        try:
            urls = self._read_oss_urls()
            if len(urls) == 1 and len(devices) > 1:
                try:
                    answer = input(
                        "检测到只有一个 OSS URL，是否让所有选中设备下载同一个 APK？输入 y 确认："
                    )
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                if answer.strip().lower() != "y":
                    self._append_log("已取消多个设备复用同一个 OSS URL 的下载安装任务")
                    return
                tasks = self._build_apk_tasks(devices, urls, reuse_single_url=True)
            elif explicit_devices and len(devices) == 1:
                tasks = [self._build_single_apk_task(devices[0], urls)]
            else:
                tasks = self._build_apk_tasks(devices, urls)
        except (OSError, ValueError) as exc:
            self._append_log(f"下载安装任务创建失败：{exc}")
            QMessageBox.warning(self, "下载安装任务创建失败", str(exc))
            return
        self._start_apk_install_queue(tasks)

    def run_airtest(self, *, devices: list[Device] | None = None):
        path = AIRTEST_SCRIPT_PATH
        if not path.is_file():
            QMessageBox.warning(self, "Airtest 脚本不存在", f"未找到内置脚本：{path}")
            return
        callback = lambda device: self.operations.run_airtest(device, path)
        process_spec_factory = self._device_process_factory("run_airtest", str(path))
        if devices is None:
            self._dispatch(
                "运行 Airtest",
                callback,
                process_spec_factory=process_spec_factory,
            )
        else:
            self._dispatch(
                "运行 Airtest",
                callback,
                devices,
                process_spec_factory=process_spec_factory,
            )

    def run_auto_login(self, *, devices: list[Device] | None = None):
        """Read native login controls and fill the bundled test credentials."""
        path = AIRTEST_AUTO_LOGIN_SCRIPT_PATH
        if not path.is_file():
            QMessageBox.warning(self, "自动填充登录脚本不存在", f"未找到内置脚本：{path}")
            return
        callback = lambda device: self.operations.run_login_autofill(device, path)
        process_spec_factory = self._device_process_factory("run_login_autofill", str(path))
        if devices is None:
            self._dispatch(
                "自动填充登录",
                callback,
                process_spec_factory=process_spec_factory,
            )
        else:
            self._dispatch(
                "自动填充登录",
                callback,
                devices,
                process_spec_factory=process_spec_factory,
            )

    def clear_packages(self, *, devices: list[Device] | None = None):
        devices = self._resolve_operation_devices(devices)
        if not devices:
            return
        answer = QMessageBox.question(
            self,
            "确认清理",
            f"将从 {len(devices)} 台设备卸载所有第三方应用，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._dispatch(
                "清理第三方包",
                self.operations.clear_third_party,
                devices,
                process_spec_factory=self._device_process_factory("clear_third_party"),
            )

    def scan_packages(self, *, devices: list[Device] | None = None):
        """Scan selected queue devices and persist discovered package labels."""
        devices = self._resolve_operation_devices(devices)
        if not devices:
            return

        mappings = [
            {
                "public_ip": device.public_ip,
                "local_port": device.local_port,
                "remote_ip": device.remote_ip,
                "remote_port": device.remote_port,
            }
            for device in devices
        ]
        reuse_serials = {
            str(mapping.get("serial") or f"127.0.0.1:{mapping['local_port']}")
            for mapping in self.tunnel_manager.active_mappings
        }

        self.thread_pool.setMaxThreadCount(self.concurrency.value())
        self._package_scan_failed_state = False
        self._append_log(f"Starting package scan for {len(devices)} selected record(s)")

        process_spec = None
        if self._can_use_package_scan_process():
            process_spec = process_spec_for_package_scan(mappings, reuse_serials)
        group_id = self._create_task_group(
            "扫描三方包",
            1,
            metadata={"devices": list(devices)},
            on_finished=self._package_scan_group_finished,
        )
        job_id = self._enqueue_task_job(
            group_id,
            [
                "global:package-scan",
                *(device_resource_key(device) for device in devices),
            ],
            lambda: self.package_inventory.collect_mappings(
                mappings,
                reuse_serials=reuse_serials,
            ),
            process_spec=process_spec,
            on_succeeded=lambda records, group_id=group_id: self._package_scan_group_succeeded(
                group_id, records
            ),
            on_failed=lambda error, group_id=group_id: self._package_scan_group_failed(
                group_id, error
            ),
        )
        self._package_scan_worker = self._job_workers.get(job_id)

    def _package_scan_group_succeeded(self, group_id: int, records: object) -> None:
        group = self._task_groups.get(group_id)
        if group is None:
            return
        self._package_scan_failed_state = False
        self._package_scan_succeeded(list(group.metadata["devices"]), records)
        if self._package_scan_failed_state:
            group.failed += 1

    def _package_scan_group_failed(self, _group_id: int, error: str) -> None:
        self._package_scan_failed_state = True
        self._append_log(f"扫描三方包 failed: {error}")

    def _package_scan_group_finished(self, _group: _UiTaskGroup) -> None:
        self._package_scan_worker = None

    def _can_use_package_scan_process(self) -> bool:
        inventory = self.package_inventory
        return (
            isinstance(inventory, PackageInventory)
            and inventory.runner is subprocess.run
            and inventory.start_tunnel is package_inventory_module.start_ssh_tunnel
            and inventory.stop_tunnel is package_inventory_module.stop_ssh_process
            and inventory.sleeper is time.sleep
            and Path(inventory.adb_path) == Path(package_inventory_module.DEFAULT_ADB_PATH)
            and inventory.ssh_user_host == package_inventory_module.DEFAULT_SSH_USER_HOST
            and Path(inventory.pem_path) == Path(package_inventory_module.DEFAULT_PEM_PATH)
            and float(inventory.tunnel_wait) == 3.0
            and inventory.remote_prefix == package_inventory_module.DEFAULT_REMOTE_PREFIX
            and int(inventory.remote_port) == package_inventory_module.DEFAULT_REMOTE_PORT
        )

    def _package_scan_succeeded(self, devices: list[Device], records: object):
        scan_errors = list(getattr(self.package_inventory, "errors", []))
        if isinstance(records, dict) and "records" in records:
            scan_errors = list(records.get("errors", []))
            records = records.get("records", [])
        grouped: dict[tuple[str, str, int], list[tuple[str, str]]] = {}
        for record in records or []:
            try:
                key = (
                    str(record.get("public_ip", "")),
                    str(record["remote_ip"]),
                    int(record["local_port"]),
                )
                package_name = str(record.get("package_name", "")).strip()
                app_name = str(record.get("app_name", "")).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if not package_name:
                continue
            entries = grouped.setdefault(key, [])
            if (app_name, package_name) not in entries:
                entries.append((app_name, package_name))

        error_keys: set[tuple[str, str, int]] = set()
        for error in scan_errors:
            try:
                error_keys.add(
                    (
                        str(error.get("public_ip", "")),
                        str(error["remote_ip"]),
                        int(error["local_port"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue

        selected_ids = {device.id for device in devices}
        updated_devices = []
        for device in self.devices:
            if device.id not in selected_ids:
                updated_devices.append(device)
                continue
            device_key = (device.public_ip, device.remote_ip, device.local_port)
            legacy_device_key = ("", device.remote_ip, device.local_port)
            entries = grouped.get(device_key, grouped.get(legacy_device_key, []))
            if not entries:
                if device_key in error_keys or legacy_device_key in error_keys:
                    updated_devices.append(device)
                else:
                    updated_devices.append(replace(device, package_name=""))
                continue
            discovered_app_names = [app_name for app_name, _ in entries if app_name]
            existing_app_name = device.app_name
            if existing_app_name.strip() == device.package_name.strip():
                existing_app_name = ""
            updated_devices.append(
                replace(
                    device,
                    app_name=(
                        "; ".join(discovered_app_names)
                        if discovered_app_names
                        else existing_app_name
                        if entries
                        else ""
                    ),
                    package_name="; ".join(package_name for _, package_name in entries),
                )
            )

        try:
            if not self._persist_devices(updated_devices, checked_ids=selected_ids):
                self._package_scan_failed_state = True
                self._append_log("Package scan result save was rejected")
                return
        except (OSError, ValueError) as exc:
            self._package_scan_failed_state = True
            self._append_log(f"Package scan result save failed: {exc}")
            QMessageBox.warning(self, "Package scan result save failed", str(exc))
            return

        for error in scan_errors:
            remote = f"{error.get('remote_ip', '')}:{error.get('local_port', '')}"
            self._append_log(f"{remote}: scan failed, {error.get('error', '')}")
        self.task_progress.setValue(len(devices))
        self._append_log(f"Package scan completed; updated {len(devices)} record(s)")

    def _package_scan_failed(self, error: str):
        self._package_scan_failed_state = True
        self.task_status.setText(f"{self._active_task_name}: failed")
        self._append_log(f"{self._active_task_name} failed: {error}")

    def _package_scan_finished(self, worker: TaskWorker):
        self._unregister_worker(worker)
        if self._package_scan_worker is worker:
            self._package_scan_worker = None
        if self._stop_requested:
            self._cleanup_task_resources()
            self.task_status.setText(f"{self._active_task_name}: 已终止")
            self._append_log(f"{self._active_task_name}已终止")
        elif not self._package_scan_failed_state:
            self.task_progress.setValue(self._task_total)
            self.task_status.setText(f"{self._active_task_name}: complete")
        for button in self._operation_buttons + [
            self.add_record_button,
            self.save_record_button,
            self.delete_record_button,
            self.connect_button,
            self.disconnect_button,
            self.generate_queue_button,
        ]:
            button.setEnabled(True)
        self.reload_button.setEnabled(True)
        self.stop_task_button.setEnabled(False)
        self._stop_requested = False

    def take_screenshots(self, *, devices: list[Device] | None = None):
        task_time = datetime.now()
        folder_name = task_time.strftime("%Y%m%d_%H%M")
        file_timestamp = task_time.strftime("%Y%m%d_%H%M%S")
        task_screenshot_dir = SCREENSHOT_DIR / folder_name

        def screenshot(device: Device):
            output_identifier = build_output_identifier(
                device.app_name,
                device.group,
                device.remote_ip,
            )
            filename = safe_filename(f"{output_identifier}_{file_timestamp}") + ".png"
            return self.operations.screenshot(device, task_screenshot_dir / filename)

        def process_screenshot(device: Device) -> ProcessTaskSpec | None:
            if not self._can_use_device_processes():
                return None
            output_identifier = build_output_identifier(
                device.app_name,
                device.group,
                device.remote_ip,
            )
            filename = safe_filename(f"{output_identifier}_{file_timestamp}") + ".png"
            return process_spec_for_device_operation(
                "screenshot",
                device,
                str(task_screenshot_dir / filename),
            )

        if devices is None:
            self._dispatch("批量截图", screenshot, process_spec_factory=process_screenshot)
        else:
            self._dispatch(
                "批量截图",
                screenshot,
                devices,
                process_spec_factory=process_screenshot,
            )

    def push_lua(self, *, devices: list[Device] | None = None):
        path = self.lua_path.text().strip()
        if not path:
            QMessageBox.warning(self, "缺少 Lua", "请选择要推送的 Lua 文件。")
            return
        destination = str(
            self.lua_destination_selector.currentData() or LUA_DESTINATION_DEFAULT
        )
        devices = self._resolve_operation_devices(devices)
        if not devices:
            return

        tasks: list[tuple[Device, str]] = []
        for row, device in enumerate(self.devices):
            if device not in devices:
                continue
            package_item = self.device_table.item(row, self.PACKAGE_NAME_COLUMN)
            package_name = package_item.text().strip() if package_item else ""
            if not package_name:
                QMessageBox.warning(self, "缺少包名", f"设备 {device.label} 未填写 APK 包名。")
                return
            try:
                build_lua_target(package_name, destination)
            except ValueError as exc:
                QMessageBox.warning(self, "包名无效", f"设备 {device.label}：{exc}")
                return
            tasks.append((device, package_name))

        self._start_lua_queue(tasks, path, destination)

    def pull_artifact(self, *, devices: list[Device] | None = None):
        artifact_type = self.pull_artifact_selector.currentText().strip()
        devices = self._resolve_operation_devices(devices)
        if not devices:
            return

        tasks: list[Device] = []
        for device in self.devices:
            if device not in devices:
                continue
            current = self._current_device_metadata(device)
            app_name = current.app_name.strip()
            package_name = current.package_name.strip()
            if not package_name:
                QMessageBox.warning(self, "缺少包名", f"设备 {device.label} 未填写 APK 包名。")
                return
            try:
                package_name = validate_package_name(package_name)
            except ValueError as exc:
                QMessageBox.warning(self, "包名无效", f"设备 {device.label}：{exc}")
                return
            tasks.append(replace(current, app_name=app_name, package_name=package_name))

        try:
            self._start_pull_queue(tasks, artifact_type)
        except ValueError as exc:
            QMessageBox.warning(self, "拉取文件", str(exc))

    def clear_sf_json(self, *, devices: list[Device] | None = None):
        devices = self._resolve_operation_devices(devices)
        if not devices:
            return

        package_names: dict[str, str] = {}
        rows_by_id = {device.id: row for row, device in enumerate(self.devices)}
        for device in devices:
            row = rows_by_id.get(device.id)
            package_item = (
                self.device_table.item(row, self.PACKAGE_NAME_COLUMN)
                if row is not None
                else None
            )
            package_name = package_item.text().strip() if package_item else ""
            if not package_name:
                QMessageBox.warning(self, "缺少包名", f"设备 {device.label} 未填写包名。")
                return
            try:
                package_names[device.id] = validate_package_name(package_name)
            except ValueError as exc:
                QMessageBox.warning(self, "包名无效", f"设备 {device.label}：{exc}")
                return

        process_spec_factory = None
        if self._can_use_device_processes():
            process_spec_factory = lambda device: process_spec_for_device_operation(
                "clear_sf_json", device, package_names[device.id]
            )
        self._dispatch(
            "清理 sf.json",
            lambda device: self.operations.clear_sf_json(device, package_names[device.id]),
            devices,
            process_spec_factory=process_spec_factory,
        )

    def push_file(self, *, devices: list[Device] | None = None):
        devices = self._resolve_operation_devices(devices)
        if not devices:
            return
        local_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择要推送的本地文件",
            str(DATA_DIR),
            "所有文件 (*)",
        )
        if not local_path:
            return
        source = Path(local_path)
        if not source.is_file():
            QMessageBox.warning(self, "本地文件无效", f"本地文件不存在：{source}")
            return
        remote_path, accepted = QInputDialog.getText(
            self,
            "远端目标路径",
            "请输入设备上的绝对目标路径：",
            QLineEdit.EchoMode.Normal,
            f"/data/local/tmp/{source.name}",
        )
        if not accepted:
            return
        try:
            safe_remote_path = validate_remote_file_path(remote_path)
        except ValueError as exc:
            QMessageBox.warning(self, "远端路径无效", str(exc))
            return

        source_path = str(source)
        self._dispatch(
            "推送文件",
            lambda device: self.operations.push_file(device, source_path, safe_remote_path),
            devices,
            process_spec_factory=self._device_process_factory(
                "push_file", source_path, safe_remote_path
            ),
        )

    def restart_apps(self, *, devices: list[Device] | None = None):
        devices = self._resolve_operation_devices(devices)
        if not devices:
            return

        package_names: dict[str, str] = {}
        for row, device in enumerate(self.devices):
            if device not in devices:
                continue
            package_item = self.device_table.item(row, self.PACKAGE_NAME_COLUMN)
            package_name = package_item.text().strip() if package_item else ""
            if not package_name:
                QMessageBox.warning(self, "缺少包名", f"设备 {device.label} 未填写 APK 包名。")
                return
            try:
                package_names[device.id] = validate_package_name(package_name)
            except ValueError as exc:
                QMessageBox.warning(self, "包名无效", f"设备 {device.label}：{exc}")
                return

        self._dispatch(
            "重启应用",
            lambda device: self.operations.restart_app(device, package_names[device.id]),
            devices,
            process_spec_factory=(
                (lambda device: process_spec_for_device_operation(
                    "restart_app", device, package_names[device.id]
                ))
                if self._can_use_device_processes()
                else None
            ),
        )

    def _start_lua_queue(
        self,
        tasks: list[tuple[Device, str]],
        lua_path: str,
        destination: str,
    ):
        if not tasks:
            return
        self._lua_queue = list(tasks)
        self._lua_queue_path = lua_path
        self._lua_queue_destination = destination
        group_id = self._create_task_group(
            "推送 Lua",
            len(tasks),
            on_finished=self._lua_group_finished,
        )
        self._append_log(f"开始推送 Lua，目标记录 {len(tasks)} 条")
        for device, package_name in tasks:
            process_spec = None
            if self._can_use_device_processes():
                process_spec = process_spec_for_device_operation(
                    "push_lua",
                    device,
                    lua_path,
                    package_name,
                    destination,
                )
            self._enqueue_task_job(
                group_id,
                [device_resource_key(device)],
                lambda device=device, package_name=package_name: self.operations.push_lua(
                    device,
                    lua_path,
                    package_name,
                    destination,
                ),
                process_spec=process_spec,
                on_succeeded=lambda result, device=device: self._operation_succeeded(
                    device, result, "推送 Lua"
                ),
                on_failed=lambda error, device=device: self._operation_failed(device, error),
            )
        self._lua_queue_worker = next(iter(self._job_workers.values()), None)

    def _push_next_lua(self):
        return None

    def _lua_worker_finished(self, worker: TaskWorker):
        return None

    def _lua_group_finished(self, _group: _UiTaskGroup) -> None:
        self._finish_lua_queue()

    def _finish_lua_queue(self):
        self._lua_queue.clear()
        self._lua_queue_worker = None
        self._lua_queue_path = ""
        self._lua_queue_destination = LUA_DESTINATION_DEFAULT

    def _start_pull_queue(
        self,
        tasks: list[Device],
        artifact_type: str,
        *,
        run_at: datetime | None = None,
    ):
        if not tasks:
            return
        if artifact_type not in PULL_ARTIFACTS:
            raise ValueError(f"不支持的拉取文件类型：{artifact_type}")
        started_at = run_at or datetime.now()
        if artifact_type == JSON_ARTIFACT:
            destinations = [
                build_json_result_directory(device.app_name, device.group, started_at)
                for device in tasks
            ]
            normalized = [str(path).casefold() for path in destinations]
            if len(normalized) != len(set(normalized)):
                raise ValueError("所选任务存在相同的应用名和区组，无法写入同一个 JSON 目录。")
            (Path(JSON_OUTPUT_DIR) / started_at.strftime("%Y%m%d")).mkdir(
                parents=True,
                exist_ok=True,
            )
        else:
            (Path(PULL_RESULT_DIR) / "lua").mkdir(parents=True, exist_ok=True)
        self._pull_queue = list(tasks)
        self._pull_queue_artifact = artifact_type
        self._pull_queue_started_at = started_at
        self._pull_queue_failed = False
        group_id = self._create_task_group(
            "拉取文件",
            len(tasks),
            metadata={"artifact_type": artifact_type},
            on_finished=self._pull_group_finished,
        )
        self._append_log(f"开始拉取 {artifact_type}，目标记录 {len(tasks)} 条")
        reserved_destinations: set[str] = set()
        first_job_id: int | None = None
        for device in tasks:
            destination = build_pull_result_directory(
                artifact_type,
                device.app_name,
                device.package_name,
                started_at,
                group=device.group,
                remote_ip=device.remote_ip,
            )
            if artifact_type != JSON_ARTIFACT:
                suffix = 2
                base_destination = destination
                while (
                    destination.exists()
                    or str(destination).casefold() in reserved_destinations
                ):
                    destination = base_destination.with_name(
                        f"{base_destination.name}_{suffix}"
                    )
                    suffix += 1
                destination.mkdir(parents=True, exist_ok=False)
            reserved_destinations.add(str(destination).casefold())

            if artifact_type == JSON_ARTIFACT:
                callback = lambda device=device, destination=destination: self.operations.pull_json_files(
                    device,
                    device.package_name,
                    destination,
                )
                process_operation = "pull_json_files"
                process_args = (device.package_name, str(destination))
            else:
                callback = lambda device=device, destination=destination: self.operations.pull_artifact(
                    device,
                    artifact_type,
                    device.package_name,
                    destination,
                )
                process_operation = "pull_artifact"
                process_args = (
                    artifact_type,
                    device.package_name,
                    str(destination),
                )
            process_spec = (
                process_spec_for_device_operation(
                    process_operation,
                    device,
                    *process_args,
                )
                if self._can_use_device_processes()
                else None
            )
            job_id = self._enqueue_task_job(
                group_id,
                [device_resource_key(device)],
                callback,
                process_spec=process_spec,
                on_succeeded=lambda result, device=device, destination=destination: self._pull_operation_succeeded(
                    device,
                    result,
                    artifact_type,
                    destination,
                ),
                on_failed=lambda error, device=device, destination=destination: self._pull_operation_failed(
                    device,
                    error,
                    artifact_type,
                    destination,
                ),
            )
            if first_job_id is None:
                first_job_id = job_id
        self._pull_queue_worker = (
            self._job_workers.get(first_job_id) if first_job_id is not None else None
        )

    def _pull_group_finished(self, group: _UiTaskGroup) -> None:
        if group.metadata.get("artifact_type") == JSON_ARTIFACT:
            cleanup_json_pull_staging(JSON_OUTPUT_DIR)
        self._pull_queue.clear()
        self._pull_queue_worker = None
        self._pull_queue_artifact = ""
        self._pull_queue_started_at = None
        self._pull_queue_failed = bool(group.failed)

    def _pull_next(self):
        if not self._pull_queue:
            self._finish_pull_queue()
            return
        device = self._pull_queue.pop(0)
        app_name = device.app_name
        package_name = device.package_name
        destination = build_pull_result_directory(
            self._pull_queue_artifact,
            app_name,
            package_name,
            self._pull_queue_started_at,
            group=device.group,
            remote_ip=device.remote_ip,
        )
        if self._pull_queue_artifact != JSON_ARTIFACT:
            suffix = 2
            while destination.exists():
                destination = destination.with_name(
                    f"{destination.name}_{suffix}"
                )
                suffix += 1
            destination.mkdir(parents=True, exist_ok=False)

        if self._pull_queue_artifact == JSON_ARTIFACT:
            callback = lambda device=device, package_name=package_name, destination=destination: self.operations.pull_json_files(
                device,
                package_name,
                destination,
            )
            process_operation = "pull_json_files"
            process_args = (package_name, str(destination))
        else:
            callback = lambda device=device, package_name=package_name, destination=destination: self.operations.pull_artifact(
                device,
                self._pull_queue_artifact,
                package_name,
                destination,
            )
            process_operation = "pull_artifact"
            process_args = (
                self._pull_queue_artifact,
                package_name,
                str(destination),
            )
        worker = TaskWorker(
            callback,
            process_spec=(
                process_spec_for_device_operation(
                    process_operation,
                    device,
                    *process_args,
                )
                if self._can_use_device_processes()
                else None
            ),
        )
        self._pull_queue_worker = worker
        self._register_worker(worker)
        worker.signals.succeeded.connect(
            lambda result, device=device, artifact_type=self._pull_queue_artifact, destination=destination:
            self._pull_operation_succeeded(device, result, artifact_type, destination)
        )
        worker.signals.failed.connect(
            lambda error, device=device, artifact_type=self._pull_queue_artifact, destination=destination:
            self._pull_operation_failed(device, error, artifact_type, destination)
        )
        worker.signals.finished.connect(lambda worker=worker: self._pull_worker_finished(worker))
        self.thread_pool.start(worker)

    def _pull_worker_finished(self, worker: TaskWorker):
        self._unregister_worker(worker)
        if self._pull_queue_worker is worker:
            self._pull_queue_worker = None
        self._task_finished += 1
        self.task_progress.setValue(self._task_finished)
        if self._pull_queue:
            QTimer.singleShot(0, self._pull_next)
        else:
            self._finish_pull_queue()

    def _finish_pull_queue(self):
        stopped = self._stop_requested
        failed = self._pull_queue_failed
        if stopped:
            self._cleanup_task_resources()
        if self._pull_queue_artifact == JSON_ARTIFACT:
            cleanup_json_pull_staging(JSON_OUTPUT_DIR)
        self._pull_queue.clear()
        self._pull_queue_worker = None
        self._pull_queue_artifact = ""
        self._pull_queue_started_at = None
        self._pull_queue_failed = False
        status = "拉取文件：已终止" if stopped else "拉取文件：失败" if failed else "拉取文件：完成"
        message = "拉取文件队列已终止" if stopped else "拉取文件队列已失败" if failed else "拉取文件队列已完成"
        self.task_status.setText(status)
        self._append_log(message)
        for button in self._operation_buttons + [
            self.add_record_button,
            self.save_record_button,
            self.delete_record_button,
            self.connect_button,
            self.disconnect_button,
            self.generate_queue_button,
        ]:
            button.setEnabled(True)
        self.reload_button.setEnabled(True)
        self.stop_task_button.setEnabled(False)
        self._stop_requested = False

    def _current_device_metadata(self, device: Device) -> Device:
        """Read editable metadata from the source row before starting a transfer."""
        row = next((index for index, item in enumerate(self.devices) if item.id == device.id), None)
        if row is None:
            return device

        def cell_text(column: int, fallback: str = "") -> str:
            item = self.device_table.item(row, column)
            return fallback if item is None else item.text().strip()

        app_name = cell_text(self.APP_NAME_COLUMN, device.app_name)
        return replace(
            device,
            app_name=app_name,
            group=cell_text(self.GROUP_COLUMN, device.group),
            package_name=cell_text(self.PACKAGE_NAME_COLUMN, device.package_name),
        )

    def move_apk(self, *, devices: list[Device] | None = None):
        """Choose a target device and move the selected source package to it."""
        selected = self._resolve_operation_devices(devices)
        if not selected:
            return
        if len(selected) != 1:
            QMessageBox.warning(self, "移动 APK", "移动 APK 只能选择一个源设备。")
            return

        source = self._current_device_metadata(selected[0])
        try:
            safe_package_name = validate_package_name(source.package_name)
        except ValueError as exc:
            QMessageBox.warning(self, "包名无效", f"源设备 {source.label}：{exc}")
            return
        source = replace(source, package_name=safe_package_name)

        targets = [device for device in self.devices if device.id != source.id]
        if not targets:
            QMessageBox.information(self, "没有目标设备", "当前队列中没有可用的目标设备。")
            return
        target_labels = [
            f"{device.id} | {device.label} | {device.remote_address}"
            for device in targets
        ]
        source_info = (
            f"源设备：{source.id} | {source.label}\n"
            f"应用名：{source.app_name or '（空）'}\n"
            f"区组：{source.group or '（空）'}\n"
            f"包名：{source.package_name}\n\n"
            "请选择目标设备："
        )
        selected_label, accepted = QInputDialog.getItem(
            self,
            "移动 APK",
            source_info,
            target_labels,
            0,
            False,
        )
        if not accepted:
            return
        try:
            target = targets[target_labels.index(selected_label)]
        except ValueError:
            QMessageBox.warning(self, "目标设备无效", "未找到所选目标设备。")
            return
        self._start_apk_move(source, target)

    def _start_apk_move(self, source: Device, target: Device):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        staging_dir = APK_TRANSFER_DIR / f"{safe_filename(source.id)}_{safe_filename(target.id)}_{timestamp}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        self._move_apk_source = source
        self._move_apk_target = target
        self._move_apk_failed_state = False
        self._append_log(
            f"开始移动 APK：{source.id}（{source.package_name}） -> {target.id}"
        )
        group_id = self._create_task_group(
            "移动 APK",
            1,
            metadata={
                "source": source,
                "target": target,
                "failed": False,
            },
            on_finished=self._move_apk_group_finished,
        )
        job_id = self._enqueue_task_job(
            group_id,
            [device_resource_key(source), device_resource_key(target)],
            lambda: self.operations.move_apk(
                source,
                target,
                source.package_name,
                staging_dir,
                progress=self.apk_transfer_progress.emit,
            ),
            on_succeeded=lambda result, group_id=group_id: self._move_apk_group_succeeded(
                group_id, result
            ),
            on_failed=lambda error, group_id=group_id: self._move_apk_group_failed(
                group_id, error
            ),
        )
        self._move_apk_worker = self._job_workers.get(job_id)

    def _move_apk_group_succeeded(self, group_id: int, result: object) -> None:
        group = self._task_groups.get(group_id)
        if group is None:
            return
        source = group.metadata.get("source")
        target = group.metadata.get("target")
        if not isinstance(source, Device) or not isinstance(target, Device):
            self._move_apk_group_failed(group_id, "移动任务缺少源设备或目标设备")
            return
        if not isinstance(result, dict):
            self._move_apk_group_failed(group_id, "移动 APK 结果格式无效")
            return
        installed_package = str(result.get("package_name", "")).strip()
        if installed_package and installed_package != source.package_name:
            self._move_apk_group_failed(
                group_id,
                f"安装包名不匹配：预期 {source.package_name}，实际 {installed_package}",
            )
            return
        updated_devices = [
            replace(
                device,
                app_name=source.app_name,
                group=source.group,
                package_name=source.package_name,
            )
            if device.id == target.id
            else device
            for device in self.devices
        ]
        try:
            self._persist_devices(updated_devices, checked_ids={target.id})
        except (OSError, ValueError) as exc:
            self._move_apk_group_failed(group_id, f"目标设备元信息保存失败：{exc}")
            return
        self._append_log(
            f"目标设备元信息已更新：{target.id}（{source.app_name} / {source.group} / {source.package_name}）"
        )

    def _move_apk_group_failed(self, group_id: int, error: str) -> None:
        group = self._task_groups.get(group_id)
        if group is not None:
            if not group.metadata.get("failed"):
                group.metadata["failed"] = True
                if group.failed == 0:
                    group.failed += 1
        self._append_log(f"移动 APK 失败：{error}")

    def _move_apk_group_finished(self, group: _UiTaskGroup) -> None:
        self._move_apk_worker = None
        self._move_apk_source = None
        self._move_apk_target = None
        self._move_apk_failed_state = bool(group.failed)

    def _move_apk_succeeded(self, result: object):
        source = self._move_apk_source
        target = self._move_apk_target
        if source is None or target is None:
            self._move_apk_failed("移动任务缺少源设备或目标设备")
            return
        if not isinstance(result, dict):
            self._move_apk_failed("移动 APK 结果格式无效")
            return
        installed_package = str(result.get("package_name", "")).strip()
        if installed_package and installed_package != source.package_name:
            self._move_apk_failed(
                f"安装包名不匹配：预期 {source.package_name}，实际 {installed_package}"
            )
            return
        updated_devices = [
            replace(
                device,
                app_name=source.app_name,
                group=source.group,
                package_name=source.package_name,
            )
            if device.id == target.id
            else device
            for device in self.devices
        ]
        try:
            self._persist_devices(updated_devices, checked_ids={target.id})
        except (OSError, ValueError) as exc:
            self._move_apk_failed(f"目标设备元信息保存失败：{exc}")
            return
        self._append_log(
            f"目标设备元信息已更新：{target.id}（{source.app_name} / {source.group} / {source.package_name}）"
        )

    def _move_apk_failed(self, error: str):
        self._move_apk_failed_state = True
        self._append_log(f"移动 APK 失败：{error}")

    def _move_apk_finished(self, worker: TaskWorker):
        self._unregister_worker(worker)
        if self._move_apk_worker is worker:
            self._move_apk_worker = None
        stopped = self._stop_requested
        if stopped:
            self._cleanup_task_resources()
            self.task_status.setText("移动 APK：已终止")
            self._append_log("移动 APK 任务已终止")
        elif self._move_apk_failed_state:
            self.task_status.setText("移动 APK：失败")
        else:
            self.task_progress.setValue(1)
            self.task_status.setText("移动 APK：完成")
            self._append_log("移动 APK 任务已完成")
        self._set_apk_controls_enabled(True)
        self._stop_requested = False
        self._move_apk_source = None
        self._move_apk_target = None
        self._move_apk_failed_state = False

    def start_scrcpy(self, *, devices: list[Device] | None = None):
        devices = self._resolve_operation_devices(devices)
        if devices:
            self._configure_scrcpy_layout(devices)
            self._start_scrcpy_queue(devices)

    def _current_screen_geometry(self) -> tuple[int, int, int, int] | None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return None
        geometry = screen.geometry()
        return geometry.x(), geometry.y(), geometry.width(), geometry.height()

    def arrange_scrcpy_windows(self):
        geometry = self._current_screen_geometry()
        if geometry is None:
            self.task_status.setText("Scrcpy：无法获取屏幕")
            self._append_log("Scrcpy 窗口排列失败：无法获取屏幕几何")
            return
        arrange = getattr(self.operations, "arrange_scrcpy_windows", None)
        if not callable(arrange):
            self.task_status.setText("Scrcpy：排列不可用")
            self._append_log("Scrcpy 窗口排列不可用")
            return
        try:
            moved = int(
                arrange(
                    geometry,
                    columns=self.scrcpy_columns.value(),
                    top_margin=self.SCRCPY_TOP_MARGIN,
                    bottom_margin=self.SCRCPY_BOTTOM_MARGIN,
                )
            )
        except Exception as exc:
            self.task_status.setText("Scrcpy：排列失败")
            self._append_log(f"Scrcpy 窗口排列失败：{exc}")
            return
        self.task_status.setText(f"Scrcpy：已排列 {moved} 个窗口")
        self._append_log(f"Scrcpy 窗口已自动排列，共 {moved} 个")

    def close_scrcpy_windows(self):
        close = getattr(self.operations, "close_scrcpy_windows", None)
        if not callable(close):
            self.task_status.setText("Scrcpy：关闭不可用")
            self._append_log("Scrcpy 窗口关闭不可用")
            return
        try:
            closed = int(close())
        except Exception as exc:
            self.task_status.setText("Scrcpy：关闭失败")
            self._append_log(f"Scrcpy 窗口关闭失败：{exc}")
            return
        self.task_status.setText(f"Scrcpy：已关闭 {closed} 个窗口")
        self._append_log(f"Scrcpy 窗口已全部关闭，共 {closed} 个")

    def _configure_scrcpy_layout(self, devices: list[Device]) -> dict[str, tuple[int, int, int, int]]:
        geometry = self._current_screen_geometry()
        if geometry is None:
            layout = {}
        else:
            rectangles = calculate_scrcpy_window_layout(
                device_count=len(devices),
                columns=self.scrcpy_columns.value(),
                screen_geometry=geometry,
                top_margin=self.SCRCPY_TOP_MARGIN,
                bottom_margin=self.SCRCPY_BOTTOM_MARGIN,
            )
            layout = {
                device.serial: rectangle
                for device, rectangle in zip(devices, rectangles)
            }

        setter = getattr(self.operations, "set_scrcpy_window_layout", None)
        if callable(setter):
            setter(layout)
        return layout

    def start_adb_shell(self, *, devices: list[Device] | None = None):
        devices = self._resolve_operation_devices(devices)
        if devices:
            self._start_adb_shell_queue(sorted(devices, key=self._remote_ip_sort_key))

    @staticmethod
    def _remote_ip_sort_key(device: Device) -> tuple[int, int | str]:
        """Sort valid remote IPs numerically while keeping malformed values last."""
        try:
            return (0, int(ipaddress.ip_address(device.remote_ip)))
        except ValueError:
            return (1, device.remote_ip)

    def _public_ip_sort_key(
        self,
        device: Device,
    ) -> tuple[int, int, int | str, int, int | str]:
        """Group named cloud servers first, preserving remote-IP order per group."""
        public_ip = device.public_ip.strip()
        try:
            server_rank = PUBLIC_IP_PRESETS.index(public_ip)
        except ValueError:
            server_rank = len(PUBLIC_IP_PRESETS)
        try:
            public_ip_key: tuple[int, int | str] = (0, int(ipaddress.ip_address(public_ip)))
        except ValueError:
            public_ip_key = (1, public_ip)
        remote_ip_key = self._remote_ip_sort_key(device)
        return (server_rank, *public_ip_key, *remote_ip_key)

    def _start_scrcpy_queue(self, devices: list[Device]):
        if not devices:
            return
        for device in devices:
            self._scrcpy_status_updated(
                {"device_id": device.id, "status": self.SCRCPY_STATUS_PENDING}
            )
        self._scrcpy_queue = list(devices)
        self._append_log(f"开始 Scrcpy 队列，目标记录 {len(devices)} 条")
        group_id = self._create_task_group(
            "Scrcpy",
            len(devices),
            on_finished=self._scrcpy_group_finished,
        )
        sequence_resource = f"sequence:scrcpy:{group_id}"
        first_job_id: int | None = None
        for index, device in enumerate(devices):
            def start(device=device, index=index):
                if index:
                    delay_ms = max(0, int(self.scrcpy_launch_delay_ms))
                    if delay_ms:
                        time.sleep(delay_ms / 1000)
                return self._start_scrcpy_with_retry(device)

            job_id = self._enqueue_task_job(
                group_id,
                [device_resource_key(device), sequence_resource],
                start,
                on_succeeded=lambda result, device=device: self._scrcpy_launch_succeeded(
                    device, result
                ),
                on_failed=lambda error, device=device: self._operation_failed(device, error),
            )
            if first_job_id is None:
                first_job_id = job_id
        self._scrcpy_queue_worker = (
            self._job_workers.get(first_job_id) if first_job_id is not None else None
        )

    def _launch_next_scrcpy(self):
        if not self._scrcpy_queue:
            self._finish_scrcpy_queue()
            return
        device = self._scrcpy_queue.pop(0)
        worker = TaskWorker(lambda device=device: self._start_scrcpy_with_retry(device))
        self._scrcpy_queue_worker = worker
        self._register_worker(worker)
        worker.signals.succeeded.connect(
            lambda result, device=device: self._scrcpy_launch_succeeded(device, result)
        )
        worker.signals.failed.connect(lambda error, device=device: self._operation_failed(device, error))
        worker.signals.finished.connect(lambda worker=worker: self._scrcpy_worker_finished(worker))
        self.thread_pool.start(worker)

    def _scrcpy_worker_finished(self, worker: TaskWorker):
        self._unregister_worker(worker)
        if self._scrcpy_queue_worker is worker:
            self._scrcpy_queue_worker = None
        self._task_finished += 1
        self.task_progress.setValue(self._task_finished)
        if self._scrcpy_queue:
            QTimer.singleShot(max(0, int(self.scrcpy_launch_delay_ms)), self._launch_next_scrcpy)
        else:
            self._finish_scrcpy_queue()

    def _scrcpy_launch_succeeded(self, device: Device, process: object) -> None:
        self._report_scrcpy_diagnostics(device, process, "启动时检查")
        QTimer.singleShot(
            max(0, int(self.scrcpy_diagnostic_delay_ms)),
            lambda: self._report_scrcpy_diagnostics(device, process, "延迟窗口检查"),
        )

    def _report_scrcpy_diagnostics(
        self, device: Device, process: object, phase: str,
    ) -> None:
        diagnose = getattr(self.operations, "scrcpy_diagnostics", None)
        if not callable(diagnose):
            return
        try:
            detail = diagnose(process)
        except Exception as exc:
            detail = f"诊断失败：{exc}"
        geometry = self._current_screen_geometry()
        if geometry is not None:
            detail += f"；当前屏幕={geometry}"
        self._scrcpy_status_updated(
            {"device_id": device.id, "status": phase, "detail": detail}
        )

    def _start_scrcpy_with_retry(self, device: Device):
        """Start Scrcpy, verify ADB, and wait for a stable process before returning."""
        retry_count = max(0, int(self.scrcpy_max_retries))
        last_error: Exception | None = None
        for attempt in range(retry_count + 1):
            process = None
            try:
                process = self.operations.start_scrcpy(device)
                self._report_scrcpy_status(
                    device, self.SCRCPY_STATUS_POPEN,
                    f"PID={getattr(process, 'pid', '未知')}，设备={device.serial}",
                )
                if not self._scrcpy_process_is_alive(process):
                    raise RuntimeError("scrcpy process exited during startup")

                check_adb_ready = getattr(self.operations, "check_adb_ready", None)
                if callable(check_adb_ready):
                    check_adb_ready(device)
                self._report_scrcpy_status(device, self.SCRCPY_STATUS_ADB_READY)
                if not self._wait_for_scrcpy_stability(process):
                    raise RuntimeError("scrcpy process exited before the stability window completed")
                self._report_scrcpy_status(device, self.SCRCPY_STATUS_STABLE)
                return process
            except Exception as exc:
                last_error = exc
                if process is not None:
                    diagnose = getattr(self.operations, "scrcpy_diagnostics", None)
                    if callable(diagnose):
                        try:
                            self._report_scrcpy_status(
                                device, "启动失败诊断", diagnose(process)
                            )
                        except Exception:
                            pass
                self._terminate_scrcpy_process(process)
                if attempt < retry_count:
                    delay_ms = max(0, int(self.scrcpy_retry_delay_ms))
                    if delay_ms:
                        time.sleep(delay_ms / 1000)

        if last_error is None:
            last_error = RuntimeError("scrcpy startup failed")
        self._report_scrcpy_status(device, self.SCRCPY_STATUS_FAILED, str(last_error))
        raise RuntimeError(
            f"scrcpy startup failed after {retry_count} retries: {last_error}"
        ) from last_error

    def _scrcpy_process_is_alive(self, process: object) -> bool:
        """Return whether a Popen-like process survived its startup window."""
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return True
        check_delay_ms = max(0, int(self.scrcpy_startup_check_ms))
        if check_delay_ms:
            time.sleep(check_delay_ms / 1000)
        try:
            return poll() is None
        except Exception:
            return False

    def _wait_for_scrcpy_stability(self, process: object) -> bool:
        """Keep the process alive for a short window before starting another device."""
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return True
        stability_ms = max(0, int(self.scrcpy_stability_check_ms))
        poll_ms = max(1, int(self.scrcpy_stability_poll_ms))
        deadline = time.monotonic() + stability_ms / 1000
        while True:
            try:
                if poll() is not None:
                    return False
            except Exception:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(poll_ms / 1000, remaining))

    @staticmethod
    def _terminate_scrcpy_process(process: object | None):
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

    def _finish_scrcpy_queue(self):
        self._scrcpy_queue.clear()
        self._scrcpy_queue_worker = None

    def _scrcpy_group_finished(self, _group: _UiTaskGroup) -> None:
        self._finish_scrcpy_queue()

    def _start_adb_shell_queue(self, devices: list[Device]):
        if not devices:
            return
        self._adb_shell_queue = list(devices)
        self._append_log(f"Starting ADB Shell queue for {len(devices)} selected record(s)")
        group_id = self._create_task_group(
            "ADB Shell",
            len(devices),
            on_finished=self._adb_shell_group_finished,
        )
        sequence_resource = f"sequence:adb-shell:{group_id}"
        first_job_id: int | None = None
        for index, device in enumerate(devices):
            def start(device=device, index=index):
                if index:
                    delay_ms = max(0, int(self.scrcpy_launch_delay_ms))
                    if delay_ms:
                        time.sleep(delay_ms / 1000)
                return self.operations.start_adb_shell(device)

            job_id = self._enqueue_task_job(
                group_id,
                [device_resource_key(device), sequence_resource],
                start,
                on_succeeded=lambda result, device=device: self._operation_succeeded(
                    device, result, "ADB Shell"
                ),
                on_failed=lambda error, device=device: self._operation_failed(device, error),
            )
            if first_job_id is None:
                first_job_id = job_id
        self._adb_shell_queue_worker = (
            self._job_workers.get(first_job_id) if first_job_id is not None else None
        )

    def _launch_next_adb_shell(self):
        if not self._adb_shell_queue:
            self._finish_adb_shell_queue()
            return
        device = self._adb_shell_queue.pop(0)
        worker = TaskWorker(lambda device=device: self.operations.start_adb_shell(device))
        self._adb_shell_queue_worker = worker
        self._register_worker(worker)
        worker.signals.succeeded.connect(lambda result, device=device: self._operation_succeeded(device, result))
        worker.signals.failed.connect(lambda error, device=device: self._operation_failed(device, error))
        worker.signals.finished.connect(lambda worker=worker: self._adb_shell_worker_finished(worker))
        self.thread_pool.start(worker)

    def _adb_shell_worker_finished(self, worker: TaskWorker):
        self._unregister_worker(worker)
        if self._adb_shell_queue_worker is worker:
            self._adb_shell_queue_worker = None
        self._task_finished += 1
        self.task_progress.setValue(self._task_finished)
        if self._adb_shell_queue:
            QTimer.singleShot(max(0, int(self.scrcpy_launch_delay_ms)), self._launch_next_adb_shell)
        else:
            self._finish_adb_shell_queue()

    def _finish_adb_shell_queue(self):
        self._adb_shell_queue.clear()
        self._adb_shell_queue_worker = None

    def _adb_shell_group_finished(self, _group: _UiTaskGroup) -> None:
        self._finish_adb_shell_queue()

    def connect_tunnels(self, *, devices: list[Device] | None = None):
        if devices is None:
            devices = self._selected_devices() or [device for device in self.devices if device.enabled]
        else:
            devices = list(devices)
        if not devices:
            QMessageBox.information(self, "队列为空", "请先新增或导入连接记录。")
            return
        selected_ids = {device.id for device in devices}
        mappings = [
            {
                "device_id": device.id,
                "public_ip": device.public_ip,
                "local_port": device.local_port,
                "remote_ip": device.remote_ip,
                "remote_port": device.remote_port,
            }
            for device in devices
        ]
        reserved_ports = {
            device.local_port
            for device in self.devices
            if device.id not in selected_ids
        }
        self._append_log(f"开始建立 {len(mappings)} 条队列连接")
        group_id = self._create_task_group(
            "建立连接",
            1,
            on_finished=self._connection_group_finished,
        )
        job_id = self._enqueue_task_job(
            group_id,
            [
                "global:tunnel-manager",
                *(device_resource_key(device) for device in devices),
            ],
            lambda: self.tunnel_manager.connect_mappings(
                mappings,
                reserved_ports=reserved_ports,
            ),
            on_succeeded=self._connection_succeeded,
            on_failed=self._connection_failed,
        )
        self._connection_worker = self._job_workers.get(job_id)

    def _connection_group_finished(self, _group: _UiTaskGroup) -> None:
        self._connection_worker = None
        self._refresh_active_connections()

    def _connection_succeeded(self, records: list[dict]):
        now = datetime.now().isoformat(timespec="seconds")
        successful_ids = []
        changed_ports = False
        checked_ids = {
            device.id
            for row, device in enumerate(self.devices)
            if (
                (item := self.device_table.item(row, self.CHECK_COLUMN)) is not None
                and item.checkState() == Qt.CheckState.Checked
            )
        }
        for record in records:
            serial = f"127.0.0.1:{record['local_port']}"
            device_id = str(record.get("device_id", "")).strip()
            device = next(
                (item for item in self.devices if item.id == device_id),
                None,
            )
            if device is None:
                device = next((item for item in self.devices if item.serial == serial), None)
            if record["adb_status"] in {"成功", "已连接"}:
                if device:
                    index = self.devices.index(device)
                    updates = {}
                    try:
                        actual_port = int(record["local_port"])
                    except (KeyError, TypeError, ValueError):
                        actual_port = device.local_port
                    if actual_port != device.local_port:
                        updates["local_port"] = actual_port
                        changed_ports = True
                    if record["adb_status"] == "成功":
                        updates.update(
                            last_connected_at=now,
                            connect_count=device.connect_count + 1,
                        )
                    if updates:
                        self.devices[index] = replace(device, **updates)
                    successful_ids.append(device.id)
                status = "已连接" if record["adb_status"] == "已连接" else "连接成功"
                self._append_log(f"{serial} -> {record['remote_ip']}:{record['remote_port']}，{status}")
            else:
                self._append_log(f"{serial}，{record.get('reason', '连接失败')}")
        if successful_ids:
            self._persist_devices(self.devices, checked_ids=checked_ids)
        if changed_ports:
            self._populate_devices(checked_ids)
        self._refresh_active_connections()
        self.task_progress.setRange(0, len(records))
        self.task_progress.setValue(len(records))
        self.task_status.setText("建立连接：完成")

    def _connection_failed(self, error: str):
        self.task_progress.setRange(0, 1)
        self.task_progress.setValue(0)
        self.task_status.setText("建立连接：失败")
        self._append_log(f"建立连接失败：{error}")
        self._refresh_active_connections()

    def _connection_finished(self):
        stopped = self._stop_requested
        if stopped:
            self._cleanup_task_resources()
        if self._connection_worker:
            self._unregister_worker(self._connection_worker)
        self._connection_worker = None
        if stopped:
            self.task_status.setText("建立连接：已终止")
            self._append_log("建立连接任务已终止")
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(True)
        self.generate_queue_button.setEnabled(True)
        for button in self._operation_buttons + [self.add_record_button, self.save_record_button, self.delete_record_button]:
            button.setEnabled(True)
        self.reload_button.setEnabled(True)
        self.stop_task_button.setEnabled(False)
        self._stop_requested = False
        self._refresh_active_connections()

    def disconnect_tunnels(self, *, devices: list[Device] | None = None):
        if devices is None:
            active = self._active_mappings()
            resource_devices = list(self.devices)

            def disconnect() -> list[dict]:
                self.tunnel_manager.disconnect()
                return active
        else:
            devices = list(devices)
            resource_devices = devices
            mappings = [
                {
                    "public_ip": device.public_ip,
                    "local_port": device.local_port,
                    "remote_ip": device.remote_ip,
                    "remote_port": device.remote_port,
                }
                for device in devices
            ]
            disconnect_mappings = getattr(self.tunnel_manager, "disconnect_mappings", None)

            def disconnect() -> list[dict]:
                if not callable(disconnect_mappings):
                    raise RuntimeError("当前连接管理器不支持单设备断开")
                result = disconnect_mappings(mappings)
                return result if isinstance(result, list) else mappings

        group_id = self._create_task_group("断开连接", 1)
        self._enqueue_task_job(
            group_id,
            [
                "global:tunnel-manager",
                *(device_resource_key(device) for device in resource_devices),
            ],
            disconnect,
            on_succeeded=self._disconnect_succeeded,
            on_failed=lambda error: self._append_log(f"断开连接失败：{error}"),
        )

    def _disconnect_succeeded(self, active: object) -> None:
        records = active if isinstance(active, list) else []
        self._refresh_active_connections()
        self._append_log(f"已断开 {len(records)} 条连接")

    def reload_queue(self):
        try:
            self.devices = load_devices(self.queue_file)
            self._refresh_public_ip_options()
            self._populate_devices()
            self._refresh_active_connections()
            self._append_log(f"已重新载入 {len(self.devices)} 条连接记录")
        except (OSError, ValueError) as exc:
            self._append_log(f"载入失败：{exc}")
            QMessageBox.warning(self, "载入失败", str(exc))

    def open_config_folder(self):
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(str(self.queue_file.parent))
        else:
            subprocess.Popen(["xdg-open", str(self.queue_file.parent)])

    def open_data_folder(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(str(DATA_DIR))
        else:
            subprocess.Popen(["xdg-open", str(DATA_DIR)])

    def open_monitor_config(self):
        path = Path(MONITOR_CONFIG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        target = path if path.is_file() else path.parent
        if hasattr(os, "startfile"):
            os.startfile(str(target))
        else:
            subprocess.Popen(["xdg-open", str(target)])

    def closeEvent(self, event):
        if hasattr(self, "_monitor_status_timer"):
            self._monitor_status_timer.stop()
        if self.stop_task_button.isEnabled():
            self.stop_current_task()
        else:
            self._cleanup_task_resources()
        self.thread_pool.waitForDone(2000)
        event.accept()
        super().closeEvent(event)


def create_application():
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    install_chinese_translations(application)
    application.setApplicationName("私服云手机管理面板")
    application.setStyle("Fusion")
    if os.name == "nt":
        application.setFont(QFont("Microsoft YaHei UI", 9))
    return application

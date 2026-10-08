from __future__ import annotations

import json
import html
import ctypes
import logging
import os
import re
import sys
import threading
import urllib.request
import time
import psutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from PySide6.QtCore import QObject, QProcess, QSettings, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeyEvent
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QMenu, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QComboBox, QGridLayout, QInputDialog, QSizePolicy, QSpinBox, QStackedWidget, QSystemTrayIcon, QTextBrowser, QVBoxLayout,
    QWidget,
)

from .app import action_manager, ask_ollama_conversation, ask_ollama_json, format_investigation, unload_model
from .brain import BrainStatus, Orchestrator
from .brain.memory import REPORT_DIR as INVESTIGATION_REPORT_DIR
from .game_monitor import REPORT_DIR as GAME_REPORT_DIR
from .game_monitor import configure_game_detector, live_game_status, system_sample, warnings_for
from .game_mode_engine import DEFAULT_GAME_MODE_SETTINGS, IntelligentGameMode, ollama_keep_alive_for_game
from .game_optimizer import configure_game_optimizer
from .router import execute_ollama_action, route_command
from .tools import READ_ONLY_TOOLS, REPAIR_TOOLS, tool_policies
from .tools.diagnostics import REPORT_DIR as DIAGNOSTIC_REPORT_DIR
from .tools.diagnostics import measure_network_jitter
from .tools.security import REPORT_DIR as SECURITY_REPORT_DIR, create_incident_report, security_health_report
from .security_presenter import (activity_text, now_label, overall_protection, security_summary,
                                 technical_json, translate_action_result)
from .bootstrap import memory_manager, permission_manager, runtime_control, self_repair_engine, skill_manager, crash_detective, execute_registered
from .config import load_settings as load_agent_settings, save_settings as save_agent_settings
from .response_formatter import format_response, technical_details, wants_technical_details
from .intent import Intent, detect_intent
from .alert_manager import alert_manager
from .ollama_manager import OllamaConfig, OllamaState, ollama_manager
from .phone_remote import configure_phone_remote, RemoteCallbacks, DEFAULT_REMOTE_SETTINGS, DEFAULT_DEVICE_PERMISSIONS, PERMISSION_LABELS
from .remote_controls import execute_remote_control, REMOTE_ACTIONS, POWER_ACTIONS


SETTINGS_FILE = Path(__file__).resolve().parent / "gui_settings.json"
LOG_FILE = DIAGNOSTIC_REPORT_DIR / "martin-ai-gui.log"
ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "martin_ai_icon.ico"
WINDOWS_APP_ID = "MartinAI.Local"
load_dotenv()
_OLLAMA_DEFAULTS = OllamaConfig.from_environment()
DEFAULTS: dict[str, Any] = {
    "model": _OLLAMA_DEFAULTS.model,
    "ollama_url": _OLLAMA_DEFAULTS.base_url,
    "poll_interval": 3,
    "auto_monitor": True,
    "minimize_to_tray": False,
    "always_on_top": False,
}

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)

STYLE = """
QWidget { background: #090909; color: #e9e9e9; font-family: 'Segoe UI'; font-size: 14px; }
QMainWindow { background: #050505; }
#sidebar, #topbar, #metricCard { background: #111111; border: 1px solid #4a0b0b; border-radius: 12px; }
#brand { color: #ff3030; font-size: 25px; font-weight: 800; }
#muted { color: #999999; }
QPushButton { background: #1b1b1b; border: 1px solid #7b1111; border-radius: 8px; padding: 9px 14px; text-align: left; }
QPushButton:hover { background: #3a0b0b; border-color: #ff3030; }
QPushButton:pressed, QPushButton:checked { background: #6f1010; }
QPushButton#primary { background: #a51414; border-color: #ff3434; font-weight: 700; text-align: center; }
QLineEdit, QPlainTextEdit, QTextBrowser, QListWidget, QSpinBox { background: #111111; border: 1px solid #4d1616; border-radius: 8px; padding: 8px; selection-background-color: #8f1717; }
QProgressBar { background: #181818; border: 1px solid #4d1616; border-radius: 7px; text-align: center; min-height: 20px; }
QProgressBar::chunk { background: #c91e1e; border-radius: 6px; }
QScrollArea { border: none; }
"""


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            settings.update({key: saved[key] for key in DEFAULTS if key in saved})
    except (OSError, json.JSONDecodeError):
        pass
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


class FunctionWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, function: Callable[[], Any]) -> None:
        super().__init__()
        self.function = function

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(self.function())
        except Exception as exc:
            logging.exception("Background task failed")
            self.failed.emit(str(exc))


class BrainSignals(QObject):
    update = Signal(str, object, object)


class ChatInput(QPlainTextEdit):
    """Chat-style editor: Enter submits; Shift+Enter keeps normal multiline editing."""
    submit_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        is_enter = event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}
        if is_enter and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            event.accept()
            self.submit_requested.emit()
            return
        super().keyPressEvent(event)


class ApprovalDialog(QDialog):
    def __init__(self, pending: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Protected action")
        self.setModal(True)
        layout = QVBoxLayout(self)
        title = QLabel("APPROVAL REQUIRED")
        title.setStyleSheet("color:#ff3030;font-size:18px;font-weight:700")
        layout.addWidget(title)
        layout.addWidget(QLabel(f"Action: {pending.get('name', 'Unknown')}"))
        details = QPlainTextEdit(json.dumps(pending.get("arguments", {}), indent=2))
        details.setReadOnly(True)
        details.setMaximumHeight(140)
        layout.addWidget(details)
        layout.addWidget(QLabel("This protected action will run only if you approve it."))
        buttons = QDialogButtonBox()
        approve = buttons.addButton("Approve", QDialogButtonBox.ButtonRole.AcceptRole)
        deny = buttons.addButton("Deny", QDialogButtonBox.ButtonRole.RejectRole)
        approve.setObjectName("primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MetricCard(QFrame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        self.title = QLabel(title)
        self.title.setObjectName("muted")
        self.value = QLabel("--")
        self.value.setStyleSheet("font-size:20px;font-weight:700;color:#ff4040")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.bar)

    def update_value(self, value: float, text: str | None = None) -> None:
        self.bar.setValue(max(0, min(100, round(value))))
        self.value.setText(text or f"{value:.0f}%")


class ResponsivePermissionPanel(QWidget):
    """Reflows permission checkboxes without shrinking their labels."""
    def __init__(self) -> None:
        super().__init__()
        self.grid = QGridLayout(self); self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(24); self.grid.setVerticalSpacing(8)
        self.widgets: list[QWidget] = []; self.columns = 0
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def set_widgets(self, widgets: list[QWidget]) -> None:
        self.widgets = widgets; self._reflow(2 if self.width() >= 500 else 1)

    def _reflow(self, columns: int) -> None:
        if columns == self.columns and self.grid.count() == len(self.widgets): return
        self.columns = columns
        for widget in self.widgets: self.grid.removeWidget(widget)
        rows = (len(self.widgets) + columns - 1) // columns
        for index, widget in enumerate(self.widgets):
            # Fill down each column so related controls remain easy to scan.
            row, column = (index % rows, index // rows) if columns == 2 else (index, 0)
            self.grid.addWidget(widget, row, column, alignment=Qt.AlignmentFlag.AlignLeft)
        for column in range(2): self.grid.setColumnStretch(column, 1 if column < columns else 0)
        self.updateGeometry()

    def resizeEvent(self, event: object) -> None:
        self._reflow(2 if self.width() >= 500 else 1)
        super().resizeEvent(event)  # type: ignore[arg-type]


class MartinMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        load_dotenv()
        self.settings = load_settings()
        self.agent_settings = load_agent_settings()
        self.history: list[dict[str, Any]] = []
        self.notification_keys: set[str] = set()
        self.threads: set[QThread] = set()
        self.chat_thread: QThread | None = None
        self.chat_cancel_event: threading.Event | None = None
        self.last_raw_response: Any = None
        self.last_investigation: Any = None
        self.brain: Orchestrator | None = None
        self.live_refresh_running = False
        self.crash_refresh_running = False
        self.refresh_phone_remote_page()
        self.ollama_refresh_running = False
        self.brain_signals = BrainSignals(self)
        self.brain_signals.update.connect(self.apply_brain_update)
        self.monitor_process: QProcess | None = None
        self.last_game: dict[str, Any] | None = None
        configure_game_detector(memory_manager)
        self.game_mode_engine = IntelligentGameMode(memory_manager, self.agent_settings.get("game_mode", {}))
        self.game_optimizer = configure_game_optimizer(runtime=runtime_control, memory=memory_manager,
                                                       settings=self.agent_settings.get("game_mode", {}))
        self.remote_history: list[dict[str, Any]] = []
        self.remote_lock = threading.RLock()
        self.phone_remote = configure_phone_remote(RemoteCallbacks(self.remote_status_snapshot, self.remote_chat, self.remote_action, self.remote_mobile_api),
                                                   settings=self.agent_settings.get("phone_remote", {}))
        self.last_ollama_game_check = 0.0
        self.ollama_repair_attempts = 0
        self.last_ollama_repair = 0.0
        self.last_game_network_probe = 0.0
        self.game_network_cache: dict[str, Any] = {"available": False}
        self.closing = False
        self.force_quit = False
        self.setWindowTitle("Martin AI")
        self.resize(1200, 760)
        self.setMinimumSize(960, 640)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._build_tray()
        self._apply_window_flags()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.refresh_live_status)
        self.poll_timer.start(int(self.settings["poll_interval"]) * 1000)
        self.refresh_live_status()
        self.refresh_reports()
        self.refresh_incident_history()
        if self.phone_remote.settings.get("enabled"):
            self.phone_remote.start(); self.refresh_phone_remote_page()
        if self.settings["auto_monitor"]:
            self.start_monitor()

    def _build_ui(self) -> None:
        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)
        self.setCentralWidget(root)

        sidebar = QFrame(objectName="sidebar")
        sidebar.setFixedWidth(205)
        side = QVBoxLayout(sidebar)
        side.addWidget(QLabel("MARTIN AI", objectName="brand"))
        side.addWidget(QLabel("LOCAL SYSTEM CORE", objectName="muted"))
        side.addSpacing(18)
        self.pages = QStackedWidget()
        for index, name in enumerate(("Chat", "Investigation", "Game Mode", "System", "Security", "Notifications", "Reports", "Memory", "Self Repair", "Phone Remote", "Skills", "Settings")):
            button = QPushButton(name)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, i=index: self.pages.setCurrentIndex(i))
            side.addWidget(button)
        side.addStretch()
        self.monitor_indicator = QLabel("● Monitor: checking")
        self.ollama_indicator = QLabel("● Ollama: checking")
        side.addWidget(self.monitor_indicator)
        side.addWidget(self.ollama_indicator)
        outer.addWidget(sidebar)

        content = QVBoxLayout()
        topbar = QFrame(objectName="topbar")
        top_stack = QVBoxLayout(topbar); top_stack.setSpacing(4)
        status_row = QGridLayout(); status_row.setHorizontalSpacing(14); status_row.setVerticalSpacing(2)
        action_row = QHBoxLayout()
        self.top_model = QLabel()
        self.top_game = QLabel("Game Mode: INACTIVE")
        self.top_gpu = QLabel("GPU: --")
        self.top_ram = QLabel("RAM: --")
        self.ai_state = QLabel("AI: MONITORING")
        self.top_remote = QLabel("Remote Control: INACTIVE")
        self.permission_badge = QLabel(f"LEVEL {int(permission_manager.current_level)}")
        status_widgets = (self.top_model, self.top_game, self.top_gpu, self.top_ram, self.top_remote, self.ai_state, self.permission_badge)
        for index, widget in enumerate(status_widgets):
            widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            status_row.addWidget(widget, index // 4, index % 4)
        status_row.setColumnStretch(3, 1)
        self.autonomy_button = QPushButton("AUTONOMY ON" if runtime_control.autonomy_enabled else "AUTONOMY OFF")
        self.autonomy_button.clicked.connect(self.toggle_autonomy)
        self.emergency_button = QPushButton("STOP AI")
        self.emergency_button.setObjectName("primary")
        self.emergency_button.setStyleSheet("background:#d00000;font-size:16px;font-weight:800")
        self.emergency_button.clicked.connect(self.emergency_stop)
        action_row.addStretch(); action_row.addWidget(self.autonomy_button); action_row.addWidget(self.emergency_button)
        top_stack.addLayout(status_row); top_stack.addLayout(action_row)
        content.addWidget(topbar)
        content.addWidget(self.pages, 1)
        outer.addLayout(content, 1)

        self.pages.addWidget(self._chat_page())
        self.pages.addWidget(self._investigation_page())
        self.pages.addWidget(self._game_page())
        self.pages.addWidget(self._system_page())
        self.pages.addWidget(self._security_page())
        self.pages.addWidget(self._notifications_page())
        self.pages.addWidget(self._reports_page())
        self.pages.addWidget(self._memory_page())
        self.pages.addWidget(self._self_repair_page())
        self.pages.addWidget(self._phone_remote_page())
        self.pages.addWidget(self._skills_page())
        self.pages.addWidget(self._settings_page())
        self.top_model.setText(f"Model: {self.settings['model']}")

    def _skills_page(self) -> QWidget:
        page = QWidget(); layout = QHBoxLayout(page)
        self.skill_list = QListWidget()
        details_box = QWidget(); details = QVBoxLayout(details_box)
        self.skill_title = QLabel("Select a skill")
        self.skill_title.setStyleSheet("font-size:24px;font-weight:700;color:#ff3434")
        self.skill_details = QPlainTextEdit(); self.skill_details.setReadOnly(True)
        self.skill_activity = QPlainTextEdit(); self.skill_activity.setReadOnly(True)
        self.skill_toggle = QPushButton("Enable / Disable")
        self.skill_toggle.clicked.connect(self.toggle_selected_skill)
        details.addWidget(self.skill_title); details.addWidget(self.skill_details, 1)
        details.addWidget(QLabel("Recent activity")); details.addWidget(self.skill_activity, 1)
        details.addWidget(self.skill_toggle)
        layout.addWidget(self.skill_list, 1); layout.addWidget(details_box, 2)
        self.skill_list.currentRowChanged.connect(self.refresh_skill_details)
        self.refresh_skills()
        return page

    def refresh_skills(self) -> None:
        if not hasattr(self, "skill_list"): return
        selected = self.skill_list.currentRow(); self.skill_list.clear()
        for item in skill_manager.information():
            self.skill_list.addItem(f"{item['name']}   {item['status']}")
        if self.skill_list.count(): self.skill_list.setCurrentRow(max(0, selected))

    def refresh_skill_details(self, row: int) -> None:
        info = skill_manager.information()
        if row < 0 or row >= len(info): return
        item = info[row]; self.skill_title.setText(item["name"])
        capabilities = "\n".join(f"- {cap['description']} (Level {cap['permission_level']})" for cap in item["capabilities"]) or "- Managed by the existing subsystem"
        self.skill_details.setPlainText(
            f"Status: {item['status']}\nVersion: {item['version']}\nRisk: {item['risk_level']}\n"
            f"Permissions: {', '.join(map(str, item['permissions']))}\n\nCapabilities:\n{capabilities}\n\n{item.get('detail', '')}")
        activity = [entry for entry in skill_manager.activity if entry["skill_id"] == item["id"]][-30:]
        self.skill_activity.setPlainText("\n".join(
            f"{entry['timestamp'][11:19]}  {entry['event'].replace('_', ' ').title()}"
            + (f" — {entry['capability']}" if entry.get("capability") else "") for entry in reversed(activity)) or "No activity yet.")
        self.skill_toggle.setText("Disable" if item["enabled"] else "Enable")

    def toggle_selected_skill(self) -> None:
        row = self.skill_list.currentRow(); info = skill_manager.information()
        if row < 0 or row >= len(info): return
        item = info[row]; enabled = not item["enabled"]
        if skill_manager.set_enabled(item["id"], enabled):
            self.agent_settings.setdefault("skills", {}).setdefault("enabled", {})[item["id"]] = enabled
            save_agent_settings(self.agent_settings); self.refresh_skills()

    def _build_tray(self) -> None:
        self.tray_icon = QSystemTrayIcon(self.windowIcon(), self)
        self.tray_icon.setToolTip("Martin AI")
        menu = QMenu(self)
        show_action = QAction("Show Martin AI", self)
        show_action.triggered.connect(self.restore_from_tray)
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(self.exit_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(
            lambda reason: self.restore_from_tray()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
        )
        if self.settings["minimize_to_tray"] and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon.show()

    def restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def exit_from_tray(self) -> None:
        self.force_quit = True
        self.close()

    def _chat_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.conversation = QTextBrowser()
        self.conversation.setOpenExternalLinks(True)
        layout.addWidget(self.conversation, 1)
        self.loading = QLabel("")
        self.loading.setStyleSheet("color:#ff3939")
        layout.addWidget(self.loading)
        row = QHBoxLayout()
        self.chat_input = ChatInput()
        self.chat_input.submit_requested.connect(self.send_message)
        self.chat_input.setPlaceholderText("Ask Martin AI or enter a fast command...")
        self.chat_input.setMaximumHeight(90)
        send = QPushButton("Send", objectName="primary")
        send.clicked.connect(self.send_message)
        clear = QPushButton("Clear chat")
        clear.clicked.connect(self.clear_chat)
        self.stop_button = QPushButton("Stop generation")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_generation)
        row.addWidget(self.chat_input, 1)
        row.addWidget(send)
        row.addWidget(clear)
        row.addWidget(self.stop_button)
        layout.addLayout(row)
        return page

    def _game_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.game_name = QLabel("No supported game is currently running.")
        self.game_name.setStyleSheet("font-size:24px;font-weight:700;color:#ff3434")
        self.game_details = QLabel("Executable: -- | PID: -- | Launcher: -- | Elapsed: --")
        self.game_path = QLabel("Installation Path: --")
        self.game_path.setWordWrap(True)
        self.game_detection_details = QLabel("Detection: Automatic | Confidence: -- | Status: Inactive")
        self.game_network = QLabel("Network: Unavailable")
        self.game_recent_event = QLabel("Recent event: No game detected")
        self.game_optimization = QLabel("Optimization: INACTIVE | Profile: Balanced | Bottleneck: None")
        self.game_optimization.setWordWrap(True)
        layout.addWidget(self.game_name)
        layout.addWidget(self.game_details)
        layout.addWidget(self.game_path)
        layout.addWidget(self.game_detection_details)
        layout.addWidget(self.game_network)
        layout.addWidget(self.game_recent_event)
        layout.addWidget(self.game_optimization)
        cards = QHBoxLayout()
        self.metric_cpu = MetricCard("CPU")
        self.metric_ram = MetricCard("RAM")
        self.metric_gpu = MetricCard("GPU")
        self.metric_vram = MetricCard("VRAM")
        for card in (self.metric_cpu, self.metric_ram, self.metric_gpu, self.metric_vram):
            cards.addWidget(card)
        layout.addLayout(cards)
        self.game_warnings = QLabel("No active warnings.")
        self.game_warnings.setWordWrap(True)
        layout.addWidget(self.game_warnings)
        buttons = QHBoxLayout()
        for text, callback in (
            ("Start monitor", self.start_monitor),
            ("Stop monitor", self.stop_monitor),
            ("Open game reports", lambda: self.open_folder(GAME_REPORT_DIR)),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        layout.addWidget(QLabel("CRASH HISTORY"))
        crash_row = QHBoxLayout()
        self.crash_history_list = QListWidget(); self.crash_history_list.setMaximumHeight(150)
        self.crash_history_details = QPlainTextEdit(); self.crash_history_details.setReadOnly(True); self.crash_history_details.setMaximumHeight(150)
        self.crash_history_list.currentRowChanged.connect(self.show_crash_history_item)
        crash_row.addWidget(self.crash_history_list, 1); crash_row.addWidget(self.crash_history_details, 2)
        layout.addLayout(crash_row)
        self.refresh_crash_history()
        return page

    def refresh_crash_history(self) -> None:
        if not hasattr(self, "crash_history_list"): return
        self._crash_history_rows = crash_detective.history(50)
        self.crash_history_list.clear()
        for row in self._crash_history_rows:
            stamp = str(row.get("timestamp") or row.get("created_at") or "")[:16].replace("T", " ")
            self.crash_history_list.addItem(f"{row.get('application', 'Application')}  |  {stamp}\n{row.get('cause', 'Unknown')} — {row.get('confidence', 'Low')}")
        if self._crash_history_rows: self.crash_history_list.setCurrentRow(0)

    def show_crash_history_item(self, row: int) -> None:
        if row < 0 or row >= len(getattr(self, "_crash_history_rows", [])): return
        item = self._crash_history_rows[row]
        events = ", ".join(map(str, item.get("event_ids", []))) or "None"
        self.crash_history_details.setPlainText(
            f"{item.get('explanation', 'No explanation available.')}\n\nConfidence: {item.get('confidence', 'Low')}\n"
            f"Repeated: {item.get('repeated_count', 1)}\n\nTechnical Details\nExecutable: {item.get('executable', 'Unavailable')}\n"
            f"Faulting module: {item.get('faulting_module') or 'Unavailable'}\nException code: {item.get('exception_code') or 'Unavailable'}\nEvent IDs: {events}")

    def _investigation_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.brain_problem = QLabel("No investigation is active.")
        self.brain_problem.setWordWrap(True)
        self.brain_problem.setStyleSheet("font-size:18px;font-weight:700;color:#ff4040")
        self.brain_status = QLabel("Status: idle")
        self.brain_output = QTextBrowser()
        self.brain_output.setOpenExternalLinks(True)
        self.brain_output.setReadOnly(True)
        self.action_log = QListWidget()
        self.action_log.setMaximumHeight(130)
        layout.addWidget(self.brain_problem)
        layout.addWidget(self.brain_status)
        layout.addWidget(self.brain_output, 1)
        layout.addWidget(QLabel("ACTION LOG"))
        layout.addWidget(self.action_log)
        row = QHBoxLayout()
        self.brain_approve = QPushButton("Approve", objectName="primary")
        self.brain_deny = QPushButton("Deny")
        self.brain_cancel = QPushButton("Cancel investigation")
        self.brain_retry = QPushButton("Retry failed step")
        self.brain_report = QPushButton("Open report")
        self.brain_approve.clicked.connect(lambda: self.resolve_brain_action(True))
        self.brain_deny.clicked.connect(lambda: self.resolve_brain_action(False))
        self.brain_cancel.clicked.connect(self.cancel_investigation)
        self.brain_retry.clicked.connect(self.retry_investigation)
        self.brain_report.clicked.connect(self.open_investigation_report)
        for button in (self.brain_approve, self.brain_deny, self.brain_cancel, self.brain_retry, self.brain_report):
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self._set_brain_buttons(False, False, False)
        return page

    def _system_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        buttons = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_system)
        save = QPushButton("Save diagnostic report", objectName="primary")
        save.clicked.connect(self.save_diagnostic_report)
        buttons.addWidget(refresh)
        buttons.addWidget(save)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.system_output = QPlainTextEdit()
        self.system_output.setReadOnly(True)
        layout.addWidget(self.system_output)
        return page

    def _reports_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.report_list = QListWidget()
        layout.addWidget(self.report_list)
        row = QHBoxLayout()
        for text, callback in (
            ("Open selected report", self.open_selected_report),
            ("Open containing folder", self.open_selected_folder),
            ("Refresh list", self.refresh_reports),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(QLabel("INCIDENT HISTORY", objectName="brand"))
        self.incident_list = QListWidget()
        layout.addWidget(self.incident_list)
        return page

    def _notifications_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Martin AI Notifications")
        heading.setStyleSheet("font-size:22px;font-weight:700;color:#ff4040")
        layout.addWidget(heading)
        self.notification_list = QListWidget()
        self.notification_list.setObjectName("notificationsTab")
        self.notification_list.addItem("No notifications yet.")
        layout.addWidget(self.notification_list, 1)
        row = QHBoxLayout()
        clear = QPushButton("Clear notifications")
        clear.clicked.connect(self.clear_notifications)
        security = QPushButton("Open Security Center", objectName="primary")
        security.clicked.connect(lambda: self.pages.setCurrentIndex(4))
        row.addWidget(clear); row.addWidget(security); row.addStretch(); layout.addLayout(row)
        return page

    def add_notification(self, title: str, message: str, severity: str = "Informational", key: str | None = None) -> bool:
        """Add one deduplicated in-app notification and mirror it to Windows."""
        identity = key or f"{severity}:{title}:{message}"
        if identity in self.notification_keys:
            return False
        self.notification_keys.add(identity)
        if self.notification_list.count() == 1 and self.notification_list.item(0).text() == "No notifications yet.":
            self.notification_list.clear()
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %I:%M:%S %p")
        self.notification_list.insertItem(0, f"[{severity}] {title} — {message} ({stamp})")
        if hasattr(self, "phone_remote"):
            category = ("Crash" if "crash" in title.lower() else "High Temperature" if "temperature" in message.lower()
                        else "Low Disk Space" if "storage" in title.lower() else "Security Alert" if "security" in title.lower() or "defender" in title.lower()
                        else "Repair Completed" if "repair" in title.lower() else "Game Detected" if "game" in title.lower()
                        else "Optimization Finished" if "optimization" in title.lower() else title)
            self.phone_remote.add_notification(category, title, message, severity=severity, key=identity)
        if QSystemTrayIcon.isSystemTrayAvailable():
            if not self.tray_icon.isVisible(): self.tray_icon.show()
            icon = QSystemTrayIcon.MessageIcon.Critical if severity in {"High", "Critical"} else QSystemTrayIcon.MessageIcon.Warning if severity == "Medium" else QSystemTrayIcon.MessageIcon.Information
            self.tray_icon.showMessage(f"Martin AI — {title}", message, icon, 8000)
        return True

    def clear_notifications(self) -> None:
        self.notification_keys.clear(); self.notification_list.clear()
        self.notification_list.addItem("No notifications yet.")

    def _security_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        self.security_overall = QLabel("○ OVERALL PROTECTION — NOT CHECKED")
        self.security_overall.setStyleSheet("font-size:22px;font-weight:700;color:#e9e9e9")
        layout.addWidget(self.security_overall)

        cards = QGridLayout(); self.security_cards: dict[str, QLabel] = {}
        for index, (key, title) in enumerate((("defender", "Windows Defender"), ("firewall", "Windows Firewall"),
                                              ("threats", "Virus & Threat Protection"), ("definitions", "Security Definitions"),
                                              ("last_scan", "Last Scan"), ("current_threats", "Current Threats"),
                                              ("processes", "Suspicious Processes"), ("network", "Network Security"))):
            card = QLabel(f"{title}\n○ Not checked"); card.setObjectName("metricCard")
            card.setWordWrap(True); card.setMinimumHeight(66); card.setMargin(10)
            cards.addWidget(card, index // 4, index % 4); self.security_cards[key] = card
        layout.addLayout(cards)

        buttons = QGridLayout()
        groups = (("SCAN", (("Quick Scan", lambda: self.queue_security_action("defender_quick_scan")),
                             ("Full Scan", lambda: self.queue_security_action("defender_full_scan")),
                             ("Scan File or Folder", self.select_security_scan_target))),
                  ("PROTECTION", (("Refresh Security Status", self.refresh_security),
                                   ("Update Defender Definitions", lambda: self.queue_security_action("update_defender_intelligence")),
                                   ("Open Windows Security", self.open_windows_security))),
                  ("INVESTIGATE", (("Suspicious Processes", self.show_security_processes),
                                    ("Network Connections", self.show_security_network))),
                  ("REPORTS", (("Security Report", self.export_security_report),
                                ("Technical Details", self.toggle_security_details))))
        for column, (group, actions) in enumerate(groups):
            box = QVBoxLayout(); heading = QLabel(group); heading.setStyleSheet("color:#ff4040;font-weight:700")
            box.addWidget(heading)
            for text, callback in actions:
                button = QPushButton(text, objectName="primary" if text == "Quick Scan" else "")
                button.clicked.connect(callback); box.addWidget(button)
            box.addStretch(); buttons.addLayout(box, 0, column)
        layout.addLayout(buttons)

        self.security_summary = QLabel("Martin AI Security Summary\nMartin AI is waiting to check Windows security.")
        self.security_summary.setObjectName("metricCard"); self.security_summary.setWordWrap(True); self.security_summary.setMargin(10)
        layout.addWidget(self.security_summary)
        self.remote_security_status = QLabel("Phone Remote: OFF"); layout.addWidget(self.remote_security_status)
        self.security_scan_progress = QProgressBar(); self.security_scan_progress.setRange(0, 100)
        self.security_scan_progress.setValue(0); self.security_scan_progress.setFormat("No security action running")
        layout.addWidget(self.security_scan_progress)
        self.security_output = QPlainTextEdit(); self.security_output.setReadOnly(True)
        self.security_output.setPlaceholderText("Recent Security Activity will appear here.")
        layout.addWidget(self.security_output, 1)
        self.security_details = QPlainTextEdit(); self.security_details.setReadOnly(True); self.security_details.setVisible(False)
        layout.addWidget(self.security_details, 1)
        self.security_snapshot: dict[str, Any] = {}; self.security_last_raw: Any = {}
        self.security_activity: list[dict[str, Any]] = []
        return page

    @Slot()
    def refresh_security(self) -> None:
        self.security_overall.setText("● OVERALL PROTECTION — CHECKING...")
        self.security_scan_progress.setRange(0, 0); self.security_scan_progress.setFormat("Checking security...")
        self.run_background(security_health_report, self.apply_security_status, self.show_error)

    @Slot(object)
    def apply_security_status(self, snapshot: object) -> None:
        data = snapshot if isinstance(snapshot, dict) else {"status": "Unavailable"}
        self.security_snapshot = data; self.security_last_raw = data
        defender = data.get("defender", {}) or {}; firewall = data.get("firewall", {}) or {}
        threats = data.get("current_threats", data.get("recent_detections")); findings = data.get("findings", []) or []
        status, explanation = overall_protection(data); icon = {"PROTECTED": "✓", "ATTENTION NEEDED": "⚠", "AT RISK": "✕"}.get(status, "○")
        color = {"PROTECTED": "#66d17a", "ATTENTION NEEDED": "#ffbf47", "AT RISK": "#ff4040"}.get(status, "#e9e9e9")
        self.security_overall.setText(f"{icon} OVERALL PROTECTION — {status}"); self.security_overall.setStyleSheet(f"font-size:22px;font-weight:700;color:{color}")
        defender_ok = defender.get("antivirus_enabled") is True and defender.get("real_time_protection_enabled") is True
        firewall_values = [firewall.get(name) for name in ("Domain", "Private", "Public")]
        firewall_ok = all(value is True for value in firewall_values)
        self.security_cards["defender"].setText("Windows Defender\n" + ("✓ Active and protecting your PC." if defender_ok else "⚠ Protection is unavailable or needs attention."))
        self.security_cards["firewall"].setText("Windows Firewall\n" + ("✓ On — network connections are being protected." if firewall_ok else "⚠ One or more firewall profiles need attention."))
        self.security_cards["threats"].setText("Virus & Threat Protection\n" + ("✓ No active threats detected." if threats == [] else "✕ Threats need review." if threats else "○ Not checked"))
        definition_text = ("⚠ Protection definitions may be out of date." if data.get("definitions_outdated") else
                           "✓ Protection definitions are up to date." if defender.get("signature_age_days") is not None else
                           "○ Update status unavailable.")
        self.security_cards["definitions"].setText("Security Definitions\n" + definition_text)
        last_scan = defender.get("last_quick_scan") or defender.get("last_full_scan")
        self.security_cards["last_scan"].setText("Last Scan\n" + (f"✓ {last_scan}" if last_scan else "○ No scan time available."))
        self.security_cards["current_threats"].setText("Current Threats\n" + (f"{'✓' if not threats else '✕'} {len(threats)} detected" if isinstance(threats, list) else "○ Not checked"))
        self.security_cards["processes"].setText("Suspicious Processes\n" + (f"⚠ {len(findings)} need review" if findings else "✓ No suspicious processes detected."))
        connections = data.get("network_connections", []) or []
        self.security_cards["network"].setText(f"Network Security\n✓ {len(connections)} active connections inventoried.")
        self.security_summary.setText(f"Martin AI Security Summary\n{security_summary(data)}\n{explanation}")
        remote = self.phone_remote.get_status()
        self.remote_security_status.setText(f"Phone Remote: {remote['security']} | {remote['address'] or 'Not listening'}")
        self.security_scan_progress.setRange(0, 100); self.security_scan_progress.setValue(100); self.security_scan_progress.setFormat("Security check complete")
        self._add_security_activity(icon, "Security status checked", status.title(), now_label())
        for finding in data.get("findings", []) or []:
            evidence = "; ".join(finding.get("evidence", [])) or "Review the available process evidence."
            self.add_notification("Suspicious process", evidence, "High" if finding.get("label") == "suspicious" else "Medium",
                                  f"process:{finding.get('pid')}:{finding.get('path')}")
        if threats:
            self.add_notification("Microsoft Defender detection", f"Defender reports {len(threats)} recent detection(s). Open Security Center to review them.", "High", f"defender-threats:{len(threats)}")
        if data.get("permission_errors"):
            self.add_notification("Security status incomplete", "Some Windows security status could not be read because of permissions or unavailable services.", "Low", "security-permission-limited")

    def queue_security_action(self, name: str, arguments: dict[str, Any] | None = None) -> None:
        queued = action_manager.queue(name, arguments or {})
        if not queued.get("pending"):
            self.show_error(str(queued.get("error", "Unable to queue protected action."))); return
        pending = action_manager.describe()
        if not pending: return
        dialog = ApprovalDialog(pending, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            action_manager.deny(); self._add_security_activity("○", "Security action cancelled", "The requested action was not run.", now_label()); return
        title = {"defender_quick_scan": "Quick Scan", "defender_full_scan": "Full Scan", "defender_custom_scan": "File or Folder Scan"}.get(name, "Security Update")
        self.security_scan_progress.setRange(0, 0); self.security_scan_progress.setFormat(f"{title} — Scanning your PC..." if "scan" in name else f"{title} in progress...")
        self.run_background(action_manager.approve, self.security_action_finished, self.security_action_failed)

    @Slot(object)
    def security_action_finished(self, result: object) -> None:
        self.security_last_raw = result
        view = translate_action_result(result)
        self.security_scan_progress.setRange(0, 100); self.security_scan_progress.setValue(100)
        self.security_scan_progress.setFormat(view["title"])
        icon = {"safe": "✓", "warning": "⚠", "error": "✕", "test": "⚠"}.get(view["status"], "○")
        self._add_security_activity(icon, view["title"], view["message"], now_label())
        self.add_notification(view["title"], view["message"].replace("\n", " "), "Informational" if view["success"] else "Medium")
        if view["executed"]: self.refresh_security()

    @Slot(str)
    def security_action_failed(self, error: str) -> None:
        self.security_scan_progress.setRange(0, 100); self.security_scan_progress.setValue(0)
        self.security_scan_progress.setFormat("Security action failed")
        self.security_last_raw = {"error": error}; self._add_security_activity("✕", "Security action couldn't start", "Martin AI couldn't complete the request. Open Technical Details for diagnostic information.", now_label())

    def select_security_scan_target(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select a file for Microsoft Defender scan")
        if not path:
            path = QFileDialog.getExistingDirectory(self, "Select a folder for Microsoft Defender scan")
        if path: self.queue_security_action("defender_custom_scan", {"path": path})

    def show_security_processes(self) -> None:
        findings = self.security_snapshot.get("findings", []) or []
        if not findings: text = "Suspicious Processes\n\n✓ No suspicious processes detected."
        else:
            rows = ["Potentially Suspicious", "Heuristic findings are not proof of malware. Review before taking action."]
            for item in findings:
                risk = "High" if item.get("label") == "suspicious" else "Medium"
                rows.append(f"\nProcess: {item.get('name') or 'Unknown application'}\nReason: {'; '.join(item.get('evidence', []))}\nRisk: {risk}\nPID: {item.get('pid', 'Unavailable')}\nActions: Investigate | Scan File | Open File Location | View Technical Details")
            text = "\n".join(rows)
        self.security_output.setPlainText(text)

    def show_security_network(self) -> None:
        connections = self.security_snapshot.get("network_connections", []) or []
        rows = ["Network Activity", "", f"{len(connections)} active connections", f"Normal: {len(connections)}", "Needs Review: 0", "", "No connection is labelled malicious without reliable evidence."]
        for item in connections[:20]:
            rows.append(f"\nApplication: {item.get('process_name') or 'Unknown'}\nRemote address: {item.get('remote_address') or 'Local/listening'}:{item.get('remote_port') or ''}\nConnection type: {item.get('protocol', 'Unknown')} — {item.get('state', 'Unknown')}\nReason flagged: Not flagged; shown for local review.")
        self.security_output.setPlainText("\n".join(rows))

    def _add_security_activity(self, icon: str, title: str, message: str, when: str) -> None:
        self.security_activity.append({"icon": icon, "title": title, "message": message, "when": when})
        self.security_activity = self.security_activity[-20:]
        self.security_output.setPlainText("Recent Security Activity\n\n" + activity_text(self.security_activity))

    def toggle_security_details(self) -> None:
        visible = not self.security_details.isVisible()
        self.security_details.setPlainText(technical_json(self.security_last_raw or self.security_snapshot))
        self.security_details.setVisible(visible)

    def open_windows_security(self) -> None:
        try:
            os.startfile("windowsdefender:")
            self._add_security_activity("✓", "Windows Security opened", "Review protection and safe actions in the Windows Security app.", now_label())
        except OSError as exc: self.show_error(f"Windows Security could not be opened: {exc}")

    def _phone_remote_page(self) -> QWidget:
        page = QWidget(); page_layout = QVBoxLayout(page); page_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(); layout = QVBoxLayout(content); layout.setContentsMargins(12, 10, 12, 18); layout.setSpacing(9)
        scroll.setWidget(content); page_layout.addWidget(scroll)
        self.phone_remote_scroll = scroll
        heading = QLabel("Phone Remote"); heading.setStyleSheet("font-size:22px;font-weight:700;color:#ff4040"); layout.addWidget(heading)
        self.remote_status_label = QLabel("Remote Server: OFF"); self.remote_status_label.setWordWrap(True); layout.addWidget(self.remote_status_label)
        self.remote_control_indicator = QLabel("Remote Control: INACTIVE"); self.remote_control_indicator.setStyleSheet("font-size:18px;font-weight:800;color:#ff4040"); layout.addWidget(self.remote_control_indicator)
        settings = {**DEFAULT_REMOTE_SETTINGS, **self.agent_settings.get("phone_remote", {})}
        self.remote_enabled_input = QCheckBox("Enable Phone Remote"); self.remote_enabled_input.setChecked(settings["enabled"])
        self.remote_chat_input = QCheckBox("Allow Remote Chat"); self.remote_chat_input.setChecked(settings["allow_chat"])
        self.remote_controls_input = QCheckBox("Allow Remote PC Controls"); self.remote_controls_input.setChecked(settings["allow_pc_controls"])
        self.remote_game_input = QCheckBox("Allow Remote Game Controls"); self.remote_game_input.setChecked(settings["allow_game_controls"])
        self.remote_input_input = QCheckBox("Allow Remote Input"); self.remote_input_input.setChecked(settings["allow_remote_input"])
        self.remote_confirm_input = QCheckBox("Require Confirmation for Power Actions"); self.remote_confirm_input.setChecked(settings["require_power_confirmation"])
        self.remote_quality_input = QComboBox(); self.remote_quality_input.addItems(("Low", "Balanced", "High")); self.remote_quality_input.setCurrentText(settings.get("remote_quality", "Balanced"))
        self.gaming_remote_quality_input = QComboBox(); self.gaming_remote_quality_input.addItems(("Low", "Balanced", "High")); self.gaming_remote_quality_input.setCurrentText(settings.get("gaming_remote_quality", "Low"))
        self.remote_timeout_input = QSpinBox(); self.remote_timeout_input.setRange(1, 120); self.remote_timeout_input.setValue(int(settings.get("control_timeout_minutes", 15)))
        options = QGridLayout(); options.setHorizontalSpacing(22); options.setVerticalSpacing(7)
        option_widgets = (self.remote_enabled_input, self.remote_chat_input, self.remote_controls_input, self.remote_game_input,
                          self.remote_input_input, self.remote_confirm_input)
        for index, item in enumerate(option_widgets): options.addWidget(item, index // 2, index % 2)
        options.setColumnStretch(0, 1); options.setColumnStretch(1, 1); layout.addLayout(options)
        configuration = QFormLayout(); configuration.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        configuration.addRow("Remote Quality", self.remote_quality_input)
        configuration.addRow("Gaming Remote Quality", self.gaming_remote_quality_input)
        configuration.addRow("Control inactivity timeout (minutes)", self.remote_timeout_input)
        layout.addLayout(configuration)
        buttons = QGridLayout(); buttons.setHorizontalSpacing(8); buttons.setVerticalSpacing(8)
        for text, callback in (("Apply", self.apply_phone_remote_settings), ("Pair New Device", self.pair_phone),
                               ("DISABLE REMOTE CONTROL", self.disable_remote_input), ("Disconnect Control", self.disconnect_remote_control),
                               ("Regenerate Access", self.revoke_all_phones)):
            button = QPushButton(text, objectName="primary" if text == "Pair New Device" else ""); button.clicked.connect(callback)
            index = buttons.count(); buttons.addWidget(button, index // 2, index % 2)
        buttons.setColumnStretch(0, 1); buttons.setColumnStretch(1, 1)
        layout.addLayout(buttons)
        self.remote_pairing_label = QLabel("No active pairing code."); self.remote_pairing_label.setWordWrap(True); layout.addWidget(self.remote_pairing_label)
        self.remote_devices = QListWidget(); self.remote_devices.setMinimumHeight(145); self.remote_devices.setMaximumHeight(220)
        self.remote_devices.currentItemChanged.connect(self.load_selected_phone_permissions); layout.addWidget(self.remote_devices)
        device_actions = QGridLayout(); device_actions.setHorizontalSpacing(8); device_actions.setVerticalSpacing(8)
        for text, callback in (("RENAME DEVICE", self.rename_selected_phone), ("DELETE DEVICE", self.delete_selected_phone),
                               ("REMOVE DUPLICATE DEVICES", self.remove_duplicate_phones),
                               ("CLEAR ALL PAIRED DEVICES", self.clear_all_phones)):
            button = QPushButton(text); button.clicked.connect(callback); index = device_actions.count()
            device_actions.addWidget(button, index // 2, index % 2)
        device_actions.setColumnStretch(0, 1); device_actions.setColumnStretch(1, 1); layout.addLayout(device_actions)
        layout.addWidget(QLabel("DEVICE PERMISSIONS"))
        self.remote_permission_inputs: dict[str, QCheckBox] = {}; permission_widgets: list[QWidget] = []
        for key, label in PERMISSION_LABELS.items():
            checkbox = QCheckBox(label); checkbox.setEnabled(False); self.remote_permission_inputs[key] = checkbox
            checkbox.setMinimumHeight(26); checkbox.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            permission_widgets.append(checkbox)
        self.remote_permission_panel = ResponsivePermissionPanel(); self.remote_permission_panel.set_widgets(permission_widgets)
        layout.addWidget(self.remote_permission_panel)
        save_permissions = QPushButton("Save Device Permissions", objectName="primary"); save_permissions.clicked.connect(self.save_selected_phone_permissions)
        layout.addWidget(save_permissions)
        revoke = QPushButton("Revoke Selected Device"); revoke.clicked.connect(self.revoke_selected_phone); layout.addWidget(revoke)
        firewall = QLabel("Firewall note: Martin AI never disables Windows Firewall or creates public rules. If access is blocked, allow the app only on Private networks.")
        firewall.setWordWrap(True); layout.addWidget(firewall); layout.addStretch()
        return page

    def _phone_settings(self) -> dict[str, Any]:
        return {"enabled": self.remote_enabled_input.isChecked(), "port": 8765,
                "allow_chat": self.remote_chat_input.isChecked(), "allow_pc_controls": self.remote_controls_input.isChecked(),
                "allow_remote_input": self.remote_input_input.isChecked(), "allow_game_controls": self.remote_game_input.isChecked(),
                "require_power_confirmation": self.remote_confirm_input.isChecked(), "remote_quality": self.remote_quality_input.currentText(),
                "gaming_remote_quality": self.gaming_remote_quality_input.currentText(), "control_timeout_minutes": self.remote_timeout_input.value()}

    def apply_phone_remote_settings(self) -> None:
        values = self._phone_settings(); self.phone_remote.settings.update(values)
        result = self.phone_remote.start() if values["enabled"] else (self.phone_remote.stop() or {"success": True})
        self.agent_settings["phone_remote"] = values; save_agent_settings(self.agent_settings); self.refresh_phone_remote_page()
        if isinstance(result, dict) and not result.get("success", True): self.show_error(str(result.get("error")))

    def pair_phone(self) -> None:
        if not self.phone_remote.server:
            self.show_error("Enable Phone Remote first."); return
        pairing = self.phone_remote.begin_pairing()
        self.remote_pairing_label.setText(f"Open {pairing['address']} on your phone. Pairing code: {pairing['code']} (expires in 5 minutes).")

    def disable_remote_input(self) -> None:
        self.phone_remote.disable_input(); self.remote_input_input.setChecked(False)
        self.agent_settings.setdefault("phone_remote", {})["allow_remote_input"] = False; save_agent_settings(self.agent_settings); self.refresh_phone_remote_page()

    def disconnect_remote_control(self) -> None:
        self.phone_remote.disconnect_control("disconnected from Martin AI desktop"); self.refresh_phone_remote_page()

    def revoke_all_phones(self) -> None:
        answer = QMessageBox.question(self, "Regenerate Phone Access",
            "Regenerating access immediately revokes every trusted phone and requires each device to pair again. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes: return
        self.phone_remote.revoke_all(); self.refresh_phone_remote_page()

    def revoke_selected_phone(self) -> None:
        item = self.remote_devices.currentItem()
        if item and item.data(Qt.ItemDataRole.UserRole): self.phone_remote.revoke(str(item.data(Qt.ItemDataRole.UserRole)))
        self.refresh_phone_remote_page()

    def _selected_phone(self) -> tuple[str, dict[str, Any]] | tuple[None, None]:
        item = self.remote_devices.currentItem(); device_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        device = next((row for row in self.phone_remote.devices if row.get("id") == device_id), None)
        return (str(device_id), device) if device_id and device else (None, None)

    def _confirm_phone_removal(self, title: str, text: str, confirm_label: str) -> bool:
        dialog = QMessageBox(self); dialog.setWindowTitle(title); dialog.setText(text); dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        confirm = dialog.addButton(confirm_label, QMessageBox.ButtonRole.DestructiveRole)
        dialog.exec(); return dialog.clickedButton() is confirm

    def delete_selected_phone(self) -> None:
        device_id, device = self._selected_phone()
        if not device_id or not device: self.show_error("Select a paired device first."); return
        if not self._confirm_phone_removal(f"Delete {device.get('name') or 'device'}?",
            "This device will lose access to Martin AI and will need to pair again.", "DELETE"): return
        if not self.phone_remote.delete_device(device_id): self.show_error("Device registration could not be deleted.")
        self.refresh_phone_remote_page()

    def rename_selected_phone(self) -> None:
        device_id, device = self._selected_phone()
        if not device_id or not device: self.show_error("Select a paired device first."); return
        name, accepted = QInputDialog.getText(self, "Rename Device", "Device name:", text=str(device.get("name") or "Phone"))
        if accepted and not self.phone_remote.rename_device(device_id, name): self.show_error("Enter a valid device name.")
        self.refresh_phone_remote_page()

    def remove_duplicate_phones(self) -> None:
        groups = self.phone_remote.duplicate_device_groups()
        if not groups: QMessageBox.information(self, "Duplicate Devices", "No duplicate device names were found."); return
        lines = []
        for group in groups:
            keep = group[0]; lines.append(f"Keep: {keep.get('name')} ({str(keep.get('id'))[:4].upper()}...{str(keep.get('id'))[-4:].upper()})")
            lines.extend(f"Remove: {row.get('name')} ({str(row.get('id'))[:4].upper()}...{str(row.get('id'))[-4:].upper()})"
                         for row in group[1:])
        if self._confirm_phone_removal("Remove Duplicate Devices?",
            "The following duplicate-name registrations were found:\n\n" + "\n".join(lines) +
            "\n\nRemoved devices will need to pair again.", "REMOVE DUPLICATES"):
            self.phone_remote.remove_duplicate_devices(); self.refresh_phone_remote_page()

    def clear_all_phones(self) -> None:
        if not self._confirm_phone_removal("Remove all paired devices?", "All phones will need to pair again.", "REMOVE ALL"): return
        self.phone_remote.clear_devices(); self.refresh_phone_remote_page()

    def load_selected_phone_permissions(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None = None) -> None:
        device_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        device = next((row for row in self.phone_remote.devices if row.get("id") == device_id and not row.get("revoked")), None)
        values = {**DEFAULT_DEVICE_PERMISSIONS, **(device.get("permissions", {}) if device else {})}
        for key, checkbox in self.remote_permission_inputs.items():
            checkbox.setEnabled(bool(device)); checkbox.setChecked(bool(values[key]) if device else False)

    def save_selected_phone_permissions(self) -> None:
        item = self.remote_devices.currentItem(); device_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not device_id: self.show_error("Select an authorized paired device first."); return
        values = {key: checkbox.isChecked() for key, checkbox in self.remote_permission_inputs.items()}
        if not self.phone_remote.update_permissions(str(device_id), values): self.show_error("Device permissions could not be saved."); return
        self.refresh_phone_remote_page()

    def refresh_phone_remote_page(self) -> None:
        if not hasattr(self, "remote_status_label"): return
        current = self.remote_devices.currentItem() if hasattr(self, "remote_devices") else None
        selected_device_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        status = self.phone_remote.get_status()
        desktop = self.phone_remote.remote_desktop_status()
        self.remote_status_label.setText(f"Remote Server: {'ON' if status['running'] else 'OFF'} | Security: {status['security']}\n"
            f"Address: {status['address'] or 'Unavailable'} | Port: {status['port']} | Paired Devices: {status['paired_devices']} | "
            f"Connected: {', '.join(status['connected_devices']) or 'None'} | Remote Input: {'ON' if status['remote_input'] else 'OFF'}")
        self.remote_control_indicator.setText(f"Remote Control: {desktop['device']}" if desktop["control_active"] else "Remote Control: INACTIVE")
        self.top_remote.setText(f"REMOTE CONTROL: {desktop['device']}" if desktop["control_active"] else "Remote Control: INACTIVE")
        self.remote_devices.clear()
        for row in status["devices"]:
            identity = str(row.get("id") or ""); short_id = f"{identity[:4].upper()}...{identity[-4:].upper()}" if len(identity) > 8 else identity.upper()
            item = QListWidgetItem(f"{row['name']}\nID: {short_id} | Paired: {row['paired_at']} | "
                                   f"Last connected: {row['last_connected']} | {'REVOKED' if row['revoked'] else 'AUTHORIZED'}")
            item.setData(Qt.ItemDataRole.UserRole, row["id"]); self.remote_devices.addItem(item)
            if row["id"] == selected_device_id: self.remote_devices.setCurrentItem(item)
        if not self.remote_devices.count(): self.remote_devices.addItem("No paired devices.")

    def remote_status_snapshot(self, _device: dict[str, Any]) -> dict[str, Any]:
        game = self.last_game or {}; sample = getattr(self, "last_live_sample", {}) or {}; gpu = sample.get("gpu", {})
        optimization = self.game_optimizer.get_optimization_status(); ollama = ollama_manager.state
        available = sample.get("ram_available_gb"); percent = sample.get("ram_percent")
        counters = psutil.net_io_counters(); now = time.monotonic(); previous = getattr(self, "_remote_network_sample", None)
        download_mbps = upload_mbps = 0.0
        if previous:
            elapsed = max(.1, now - previous[0]); download_mbps = max(0.0, (counters.bytes_recv - previous[1]) * 8 / elapsed / 1_000_000)
            upload_mbps = max(0.0, (counters.bytes_sent - previous[2]) * 8 / elapsed / 1_000_000)
        self._remote_network_sample = (now, counters.bytes_recv, counters.bytes_sent)
        total_ram = float(available) / max(.01, 1 - float(percent) / 100) if isinstance(available, (int, float)) and isinstance(percent, (int, float)) else None
        return {"online": True, "cpu": sample.get("cpu_percent"), "cpu_temperature": sample.get("cpu_temperature_c"), "gpu": gpu.get("utilization_percent"),
                "gpu_temperature": gpu.get("temperature_c"), "ram": sample.get("ram_percent"),
                "ram_available_gb": available, "ram_total_gb": round(total_ram, 1) if total_ram else None,
                "storage_free_gb": round(sum(float(row.get("free_gb", 0)) for row in sample.get("disks", [])), 1),
                "network_download_mbps": round(download_mbps, 2), "network_upload_mbps": round(upload_mbps, 2),
                "ollama_status": ollama.status, "model": ollama.selected_model or self.settings["model"],
                "autonomy": runtime_control.autonomy_enabled, "martin_status": "STOPPED" if runtime_control.emergency_stopped else "ONLINE",
                "game": game.get("game"), "game_active": bool(game), "game_session_seconds": game.get("elapsed_seconds", 0),
                "optimization": optimization, "remote_control": self.phone_remote.remote_desktop_status()}

    def remote_mobile_api(self, resource: str, body: dict[str, Any], device: dict[str, Any]) -> dict[str, Any]:
        if resource == "dashboard": return {"success": True, **self.remote_status_snapshot(device)}
        if resource == "skills":
            skill_id, enabled = str(body.get("skill_id", "")), body.get("enabled")
            if skill_id and enabled is not None:
                if skill_id in {"remote", "security"}: return {"success": False, "error": "This safety-critical skill must be changed on Martin PC."}
                if not skill_manager.set_enabled(skill_id, bool(enabled)): return {"success": False, "error": "Skill unavailable"}
                self.agent_settings.setdefault("skills", {}).setdefault("enabled", {})[skill_id] = bool(enabled)
                save_agent_settings(self.agent_settings)
            return {"success": True, "skills": skill_manager.information(), "activity": skill_manager.activity[-50:]}
        if resource == "crashes": return {"success": True, "incidents": crash_detective.history(50)}
        if resource == "apps":
            if body.get("operation") == "open": return execute_registered("open_app", {"app_name": str(body.get("app_name", ""))}, trigger="phone_remote")
            if body.get("operation") == "close": return action_manager.queue("close_app", {"app_name": str(body.get("app_name", ""))})
            return READ_ONLY_TOOLS["list_running_apps"](limit=50)
        return {"success": False, "error": "Unknown mobile resource"}

    def remote_chat(self, message: str, _device: dict[str, Any]) -> dict[str, Any]:
        from .investigation_followup import answer_investigation_followup
        followup = answer_investigation_followup(message, self.last_investigation)
        if followup is not None:
            return {"success": True, "message": followup, "intent": "investigation_followup"}
        routed = route_command(message, action_manager, memory_manager,
                               bool(self.agent_settings.get("memory", {}).get("automatic_enabled", True)), self_repair_engine)
        if routed.handled: return {"success": True, "message": routed.message, "intent": routed.intent}
        with self.remote_lock:
            self.remote_history.append({"role": "user", "content": message})
            memories = memory_manager.relevant_memories(message, limit=int(self.agent_settings.get("memory", {}).get("retrieval_limit", 5)))
            try:
                answer = ask_ollama_conversation(str(self.settings["ollama_url"]).rstrip("/"), str(self.settings["model"]),
                                                 self.remote_history[-7:], ollama_keep_alive_for_game(bool(self.game_mode_engine.active)), memories=memories)
            except Exception as exc: return {"success": False, "error": f"Local conversation service unavailable: {exc}"}
            self.remote_history.append({"role": "assistant", "content": answer}); return {"success": True, "message": answer}

    def remote_action(self, action: str, confirmed: bool, _device: dict[str, Any]) -> dict[str, Any]:
        if action not in REMOTE_ACTIONS: return {"success": False, "error": "Remote action is not allowlisted"}
        if action == "stop_ai":
            if not confirmed: return {"success": False, "error": "Confirmation is required"}
            runtime_control.stop(); self.phone_remote.disable_input(); self.agent_settings.setdefault("phone_remote", {})["allow_remote_input"] = False
            save_agent_settings(self.agent_settings); action_manager.deny() if action_manager.has_pending() else None
            self_repair_engine.cancel_pending(); return {"success": True, "message": "STOP AI activated; remote input disabled"}
        if action in POWER_ACTIONS and self.phone_remote.settings.get("require_power_confirmation") and not confirmed:
            return {"success": False, "error": "Power action confirmation is required"}
        # This is an authenticated, explicitly authorized user request, not an autonomous AI decision.
        if runtime_control.emergency_stopped: return {"success": False, "error": "STOP AI is active"}
        return execute_remote_control(action)

    def export_security_report(self) -> None:
        result = create_incident_report(self.security_snapshot or {"status": "Not checked"})
        self.security_last_raw = result
        if result.get("success"):
            self._add_security_activity("✓", "Security report exported", f"Saved to {result.get('path', 'the reports folder')}", now_label())
        else:
            self._add_security_activity("✕", "Security report couldn't be exported", "Open Technical Details for diagnostic information.", now_label())
        self.refresh_reports()

    def _settings_page(self) -> QWidget:
        page = QWidget(); outer = QVBoxLayout(page); outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(); form = QFormLayout(content); form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setContentsMargins(12, 10, 12, 18); scroll.setWidget(content); outer.addWidget(scroll)
        self.model_input = QLineEdit(str(self.settings["model"]))
        self.url_input = QLineEdit(str(self.settings["ollama_url"]))
        self.poll_input = QSpinBox()
        self.poll_input.setRange(1, 60)
        self.poll_input.setValue(int(self.settings["poll_interval"]))
        self.auto_monitor_input = QCheckBox()
        self.auto_monitor_input.setChecked(bool(self.settings["auto_monitor"]))
        self.tray_input = QCheckBox()
        self.tray_input.setChecked(bool(self.settings["minimize_to_tray"]))
        self.always_input = QCheckBox()
        self.always_input.setChecked(bool(self.settings["always_on_top"]))
        self.simulation_input = QCheckBox()
        self.simulation_input.setChecked(runtime_control.simulation_mode)
        self.permission_input = QComboBox()
        self.permission_input.addItems(("Level 1 — Observe", "Level 2 — Safe Automation", "Level 3 — Ask First", "Level 4 — Administrator"))
        self.permission_input.setCurrentIndex(int(permission_manager.current_level) - 1)
        game_settings = {**DEFAULT_GAME_MODE_SETTINGS, **self.agent_settings.get("game_mode", {})}
        self.game_detection_input = QCheckBox(); self.game_detection_input.setChecked(game_settings["automatic_detection"])
        self.game_auto_enable_input = QCheckBox(); self.game_auto_enable_input.setChecked(game_settings["auto_enable"])
        self.game_performance_input = QCheckBox(); self.game_performance_input.setChecked(game_settings["performance_monitoring"])
        self.game_crash_input = QCheckBox(); self.game_crash_input.setChecked(game_settings["crash_detection"])
        self.game_reports_input = QCheckBox(); self.game_reports_input.setChecked(game_settings["session_reports"])
        self.game_notifications_input = QCheckBox(); self.game_notifications_input.setChecked(game_settings["performance_notifications"])
        self.game_optimization_level_input = QComboBox(); self.game_optimization_level_input.addItems(("Conservative", "Balanced", "Maximum Safe"))
        self.game_optimization_level_input.setCurrentText(game_settings["optimization_level"])
        self.game_ai_resource_input = QCheckBox(); self.game_ai_resource_input.setChecked(game_settings["ai_resource_mode"])
        self.game_idle_unload_input = QCheckBox(); self.game_idle_unload_input.setChecked(game_settings["allow_idle_model_unload"])
        form.addRow("Ollama model", self.model_input)
        form.addRow("Ollama URL", self.url_input)
        form.addRow("Poll interval (seconds)", self.poll_input)
        form.addRow("Start game monitor automatically", self.auto_monitor_input)
        form.addRow("Minimize to tray", self.tray_input)
        form.addRow("Always on top", self.always_input)
        form.addRow("Simulation mode (zero system changes)", self.simulation_input)
        form.addRow("Permission level", self.permission_input)
        form.addRow("Automatic Game Detection", self.game_detection_input)
        form.addRow("Automatically Enable Game Mode", self.game_auto_enable_input)
        form.addRow("Performance Monitoring", self.game_performance_input)
        form.addRow("Crash Detection", self.game_crash_input)
        form.addRow("Session Reports", self.game_reports_input)
        form.addRow("Performance Notifications", self.game_notifications_input)
        form.addRow("Game Optimization Mode", self.game_optimization_level_input)
        form.addRow("AI Gaming Resource Mode", self.game_ai_resource_input)
        form.addRow("Allow idle model unload", self.game_idle_unload_input)
        save = QPushButton("Save settings", objectName="primary")
        save.clicked.connect(self.store_settings)
        form.addRow(save)
        return page

    def _self_repair_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        heading = QLabel("Safe Self Repair"); heading.setStyleSheet("font-size:22px;font-weight:700;color:#ff4040")
        layout.addWidget(heading)
        self.self_repair_enabled_input = QCheckBox("Self Repair ON")
        self.self_repair_enabled_input.setChecked(self_repair_engine.enabled)
        self.self_repair_auto_input = QCheckBox("Automatically perform Level 1 repairs")
        self.self_repair_auto_input.setChecked(self_repair_engine.auto_level_1)
        self.self_repair_enabled_input.toggled.connect(self.save_self_repair_options)
        self.self_repair_auto_input.toggled.connect(self.save_self_repair_options)
        layout.addWidget(self.self_repair_enabled_input); layout.addWidget(self.self_repair_auto_input)
        self.repair_summary = QLabel(); self.repair_summary.setWordWrap(True); layout.addWidget(self.repair_summary)
        self.repair_history_list = QListWidget(); layout.addWidget(self.repair_history_list, 1)
        row = QHBoxLayout(); refresh = QPushButton("Refresh history"); refresh.clicked.connect(self.refresh_repair_history)
        approve = QPushButton("Approve pending repair", objectName="primary"); approve.clicked.connect(lambda: self.resolve_pending_repair(True))
        deny = QPushButton("Deny pending repair"); deny.clicked.connect(lambda: self.resolve_pending_repair(False))
        for button in (refresh, approve, deny): row.addWidget(button)
        row.addStretch(); layout.addLayout(row); self.refresh_repair_history(); return page

    def save_self_repair_options(self) -> None:
        if not hasattr(self, "self_repair_enabled_input"): return
        enabled = self.self_repair_enabled_input.isChecked(); auto = self.self_repair_auto_input.isChecked()
        self_repair_engine.set_options(enabled=enabled, auto_level_1=auto)
        self.agent_settings.setdefault("self_repair", {}).update({"enabled": enabled, "auto_level_1": auto})
        save_agent_settings(self.agent_settings)

    def resolve_pending_repair(self, approved: bool) -> None:
        result = self_repair_engine.approve() if approved else self_repair_engine.deny()
        self.append_message("Martin AI", result.get("message", "Repair request updated."), False)
        self.refresh_repair_history()

    def refresh_repair_history(self) -> None:
        if not hasattr(self, "repair_history_list"): return
        self.repair_history_list.clear(); history = memory_manager.repair_history(50)
        pending = self_repair_engine.pending
        self.repair_summary.setText(
            ("Pending approval: " + self_repair_engine.plan_text()) if pending else
            "Pending approvals: none\nLast diagnostic and repair results are shown below."
        )
        for item in history:
            verification = item.get("verification") or {}
            self.repair_history_list.addItem(
                f"{item['timestamp']} | {item.get('repair_id') or 'diagnostic'} | Level {item['permission_level']} | "
                f"{item['status']} | Verified: {'yes' if verification.get('success') else 'no'} | {item['problem']}"
            )
        if not history: self.repair_history_list.addItem("No repair history yet.")

    def _memory_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        heading = QLabel("Martin AI Memory"); heading.setStyleSheet("font-size:22px;font-weight:700;color:#ff4040")
        layout.addWidget(heading)
        row = QHBoxLayout(); self.memory_search = QLineEdit(); self.memory_search.setPlaceholderText("Search saved memories...")
        search = QPushButton("Search"); search.clicked.connect(self.refresh_memories)
        row.addWidget(self.memory_search, 1); row.addWidget(search); layout.addLayout(row)
        self.memory_list = QListWidget(); layout.addWidget(self.memory_list, 1)
        controls = QHBoxLayout(); delete = QPushButton("Delete selected"); delete.clicked.connect(self.delete_selected_memory)
        clear = QPushButton("Clear all memories", objectName="primary"); clear.clicked.connect(self.confirm_clear_memories)
        self.auto_memory_input = QCheckBox("Enable automatic memory")
        self.auto_memory_input.setChecked(bool(self.agent_settings.get("memory", {}).get("automatic_enabled", True)))
        self.auto_memory_input.toggled.connect(self.set_automatic_memory)
        controls.addWidget(delete); controls.addWidget(clear); controls.addWidget(self.auto_memory_input); controls.addStretch()
        layout.addLayout(controls); self.refresh_memories(); return page

    def refresh_memories(self) -> None:
        if not hasattr(self, "memory_list"): return
        self.memory_list.clear()
        for item in memory_manager.list_memories(search=self.memory_search.text() if hasattr(self, "memory_search") else "", limit=200):
            widget = QListWidgetItem(f"[{item['category'].replace('_', ' ').title()}] {item['content']}")
            widget.setData(Qt.ItemDataRole.UserRole, item["id"]); self.memory_list.addItem(widget)
        if not self.memory_list.count(): self.memory_list.addItem("No saved memories.")

    def delete_selected_memory(self) -> None:
        item = self.memory_list.currentItem()
        if item and item.data(Qt.ItemDataRole.UserRole): memory_manager.delete_memory(int(item.data(Qt.ItemDataRole.UserRole)))
        self.refresh_memories()

    def confirm_clear_memories(self) -> None:
        if QMessageBox.question(self, "Clear memories", "Permanently delete every saved memory?") == QMessageBox.StandardButton.Yes:
            memory_manager.clear_memories(); self.refresh_memories()

    def set_automatic_memory(self, enabled: bool) -> None:
        self.agent_settings.setdefault("memory", {})["automatic_enabled"] = bool(enabled)
        save_agent_settings(self.agent_settings)

    def run_background(
        self,
        function: Callable[[], Any],
        success: Callable[[Any], None],
        failure: Callable[[str], None] | None = None,
    ) -> QThread:
        thread = QThread(self)
        worker = FunctionWorker(function)
        # Keep the Python wrapper alive for the lifetime of its QThread.
        thread.worker = worker  # type: ignore[attr-defined]
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(success)
        worker.failed.connect(failure or self.show_error)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: self.threads.discard(thread))
        self.threads.add(thread)
        thread.start()
        return thread

    @Slot()
    def send_message(self) -> None:
        text = self.chat_input.toPlainText().strip()
        if not text or self.chat_thread is not None:
            return
        self.chat_input.clear()
        self.chat_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.append_message("You", text, True)
        if wants_technical_details(text):
            self.append_message("Martin AI", technical_details(self.last_raw_response), False)
            return
        automatic_memory = bool(self.agent_settings.get("memory", {}).get("automatic_enabled", True))
        from .investigation_followup import answer_investigation_followup
        followup = answer_investigation_followup(text, self.last_investigation)
        if followup is not None:
            self.history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": followup}])
            self.append_message("Martin AI", followup, False)
            return
        routed = route_command(text, action_manager, memory_manager, automatic_memory, self_repair_engine)
        if routed.handled:
            if routed.raw_result is not None:
                self.last_raw_response = routed.raw_result
            self.append_message("Martin AI", routed.message, False)
            if routed.intent == "ollama_action":
                operation = str((routed.raw_result or {}).get("operation", "status"))
                ollama_manager.configure(base_url=str(self.settings["ollama_url"]), model=str(self.settings["model"]))
                self.apply_ollama_status(OllamaState(
                    "reconnecting" if operation in {"reconnect", "repair"} else
                    ("starting" if operation in {"start", "restart"} else ollama_manager.state.status),
                    ollama_manager.is_ollama_installed(), ollama_manager.find_executable(),
                ))
                self.chat_thread = self.run_background(
                    lambda: execute_ollama_action(operation, ollama_manager),
                    self.ollama_action_finished, self.ollama_action_failed,
                )
                return
            if routed.intent == "memory": self.refresh_memories()
            if routed.intent == "self_repair": self.refresh_repair_history()
            self.handle_pending_action()
            return
        intent = detect_intent(text)
        if intent in {Intent.GENERAL_CHAT, Intent.UNKNOWN}:
            self.start_conversation(text)
        else:
            self.start_investigation(text)

    def start_conversation(self, text: str) -> None:
        self.loading.setText("Martin AI is thinking...")
        self.stop_button.setEnabled(True)
        self.history.append({"role": "user", "content": text})
        relevant = memory_manager.relevant_memories(text, limit=int(self.agent_settings.get("memory", {}).get("retrieval_limit", 5)))
        self.chat_cancel_event = threading.Event()
        model = str(self.settings["model"])
        base_url = str(self.settings["ollama_url"]).rstrip("/")
        keep_alive = ollama_keep_alive_for_game(bool(self.game_mode_engine.active))
        self.chat_thread = self.run_background(
            lambda: ask_ollama_conversation(base_url, model, self.history[-7:], keep_alive, self.chat_cancel_event, relevant),
            self.chat_finished, self.conversation_failed,
        )

    @Slot(object)
    def ollama_action_finished(self, result: object) -> None:
        state, message = result  # type: ignore[misc]
        self.last_raw_response = state
        self.apply_ollama_status(state)
        self.append_message("Martin AI", str(message), False)
        self._finish_chat()

    @Slot(str)
    def ollama_action_failed(self, error: str) -> None:
        self.apply_ollama_status(OllamaState("error", message=str(error)))
        self.append_message("Martin AI", "I couldn't complete the Ollama action.", False)
        self._finish_chat()

    def start_investigation(self, text: str) -> None:
        self.loading.setText("Planning investigation...")
        self.stop_button.setEnabled(True)
        self.brain_problem.setText(text)
        self.action_log.clear(); self.action_log.addItem("Planning safe checks")
        model = str(self.settings["model"])
        base_url = str(self.settings["ollama_url"]).rstrip("/")
        keep_alive = ollama_keep_alive_for_game(bool(self.game_mode_engine.active))
        self.brain = Orchestrator(
            READ_ONLY_TOOLS, REPAIR_TOOLS, tool_policies(), action_manager,
            lambda prompt: ask_ollama_json(base_url, model, prompt, keep_alive),
            memory=memory_manager,
        )
        callback = lambda event, state, step: self.brain_signals.update.emit(event, state, step)
        self.chat_thread = self.run_background(
            lambda: self.brain.start(text, {"game": self.last_game, "conversation": self.history[-6:],
                "relevant_memories": [item["content"] for item in memory_manager.relevant_memories(text, limit=5)]}, callback),
            self.brain_finished, self.chat_failed,
        )

    @Slot(str, object, object)
    def apply_brain_update(self, event: str, state: object, step: object) -> None:
        if self.closing:
            return
        label = event.replace("_", " ").capitalize()
        if step is not None:
            label = f"Step {getattr(step, 'step_id', '?')}: {getattr(step, 'goal', '')}"
        self.loading.setText(label)
        self.brain_status.setText(f"Status: {getattr(state, 'status', 'unknown')} | {label}")
        self.brain_output.setMarkdown(self._brain_detail(state))
        action = {
            "step_started": f"Checking: {getattr(step, 'goal', 'system information')}",
            "step_completed": f"Checked: {getattr(step, 'goal', 'system information')}",
            "step_failed": f"Check unavailable: {getattr(step, 'goal', 'system information')}",
            "approval_required": f"Approval requested: {getattr(step, 'goal', 'protected action')}",
            "evaluating": "Analyzing collected results",
            "completed": "Recommendation generated",
        }.get(event)
        if action: self.action_log.addItem(action)

    @Slot(object)
    def brain_finished(self, state: object) -> None:
        if self.closing:
            return
        self.brain_output.setMarkdown(self._brain_detail(state))
        self.last_raw_response = state
        status = getattr(state, "status", "failed")
        waiting = status == BrainStatus.WAITING_FOR_APPROVAL.value
        complete = status == BrainStatus.COMPLETED.value
        if complete:
            self.last_investigation = state
        failed = any(getattr(step, "status", "") == "failed" for step in getattr(state, "active_plan", []))
        self._set_brain_buttons(waiting, failed, complete)
        if waiting:
            pending = getattr(state, "pending_action", {}) or {}
            self.append_message("Martin AI", format_response({"pending": True, "name": pending.get("name")}), False)
        elif complete:
            answer = format_investigation(state)
            self.history.extend([{"role": "user", "content": getattr(state, "user_request", "")}, {"role": "assistant", "content": answer}])
            self.append_message("Martin AI", answer, False)
        self._finish_chat()

    def _brain_detail(self, state: object) -> str:
        sections = [format_response(state, {"kind": "autonomy"})]
        plan = getattr(state, "active_plan", [])
        if plan:
            sections.append("**Plan**\n\n" + "\n".join(
                f"{index}. {step.goal} — {step.status.replace('_', ' ')}"
                for index, step in enumerate(plan, 1)))
        evidence = []
        for item in getattr(state, "evidence", []):
            tool, result = item.get("tool"), item.get("result", {})
            if not isinstance(result, dict):
                continue
            if tool == "system_summary" and isinstance(result.get("ram_percent_used"), (int, float)):
                ram = result["ram_percent_used"]
                condition = "high memory pressure" if ram >= 90 else "below the 90% high-pressure threshold"
                evidence.append(f"RAM: {ram:g}% used; {condition}.")
            elif tool == "recent_windows_errors" and isinstance(result.get("events"), list):
                evidence.append(f"Windows: {len(result['events'])} recent warning/error events; a cause is not confirmed by the count alone.")
            elif tool == "top_processes":
                for process in result.get("highest_memory", [])[:3]:
                    if isinstance(process.get("memory_mb"), (int, float)):
                        evidence.append(f"Memory use: {process.get('name', 'Application')} — {process['memory_mb']:g} MB.")
        if evidence:
            sections.append("**Evidence**\n\n" + "\n".join(f"- {line}" for line in evidence))
        if getattr(state, "diagnosis", ""):
            sections.append(f"**Confidence:** {getattr(state, 'confidence', 'low')}")
        uncertainty = getattr(state, "uncertainty", [])
        if uncertainty:
            sections.append("**Uncertainty**\n\n" + "\n".join(f"- {line}" for line in uncertainty))
        history = getattr(state, "related_history", [])
        if history:
            lines = []
            for case in history:
                if case.get("kind") == "repair":
                    outcome = "verified after repair" if case.get("verified") else "not verified as a fix"
                    lines.append(f"{case.get('problem', '')}: {case.get('repair_id', 'repair')} — {outcome}.")
                else:
                    lines.append(f"{case.get('problem', '')}: {case.get('diagnosis', 'Prior investigation recorded.')}")
            sections.append("**Similar Past Cases**\n\nHistorical context; fresh checks are still required.\n\n"
                            + "\n".join(f"- {line}" for line in lines))
        return "\n\n".join(sections)

    def _set_brain_buttons(self, waiting: bool, failed: bool, report: bool) -> None:
        self.brain_approve.setEnabled(waiting)
        self.brain_deny.setEnabled(waiting)
        self.brain_cancel.setEnabled(bool(self.brain) and not report)
        self.brain_retry.setEnabled(failed and not waiting)
        self.brain_report.setEnabled(report)

    def resolve_brain_action(self, approved: bool) -> None:
        if not self.brain or self.chat_thread is not None:
            return
        callback = lambda event, state, step: self.brain_signals.update.emit(event, state, step)
        operation = self.brain.approve if approved else self.brain.deny
        self.chat_thread = self.run_background(lambda: operation(callback), self.brain_finished, self.chat_failed)

    def cancel_investigation(self) -> None:
        if self.brain:
            state = self.brain.cancel(lambda event, value, step: self.brain_signals.update.emit(event, value, step))
            self.brain_output.setMarkdown(self._brain_detail(state))
            self._set_brain_buttons(False, False, False)

    def retry_investigation(self) -> None:
        if not self.brain or self.chat_thread is not None:
            return
        callback = lambda event, state, step: self.brain_signals.update.emit(event, state, step)
        self.chat_thread = self.run_background(lambda: self.brain.retry_failed(callback), self.brain_finished, self.chat_failed)

    def open_investigation_report(self) -> None:
        if self.brain and self.brain.state:
            path = self.brain.state.report_paths.get("markdown")
            if path and Path(path).exists():
                os.startfile(path)

    def append_message(self, sender: str, text: str, user: bool) -> None:
        color = "#ff4545" if user else "#e6e6e6"
        align = "right" if user else "left"
        safe = html.escape(str(text))
        safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
        safe = safe.replace("\n", "<br>")
        self.conversation.append(
            f'<div style="text-align:{align};margin:10px"><b style="color:{color}">{sender}</b><br>{safe}</div>'
        )

    @Slot(object)
    def chat_finished(self, answer: object) -> None:
        if self.closing:
            return
        self.last_raw_response = answer
        text = format_response(answer, {"kind": "chat"})
        self.history.append({"role": "assistant", "content": text})
        self.append_message("Martin AI", text, False)
        self._finish_chat()
        self.handle_pending_action()

    @Slot(str)
    def conversation_failed(self, _error: str) -> None:
        cancelled = bool(self.chat_cancel_event and self.chat_cancel_event.is_set())
        if self.history and self.history[-1].get("role") == "user": self.history.pop()
        message = "Generation stopped." if cancelled else "Local AI couldn't answer right now. Please try again."
        self.append_message("Martin AI", message, False)
        self._finish_chat()

    @Slot(str)
    def chat_failed(self, error: str) -> None:
        if self.history and self.history[-1].get("role") == "user":
            self.history.pop()
        self.last_raw_response = {"success": False, "error": error}
        self.append_message("Martin AI", format_response(self.last_raw_response), False)
        self._finish_chat()

    def _finish_chat(self) -> None:
        self.loading.clear()
        self.stop_button.setEnabled(False)
        self.chat_thread = None
        self.chat_cancel_event = None

    def stop_generation(self) -> None:
        if self.brain and self.brain.state and self.brain.state.status not in {BrainStatus.COMPLETED.value, BrainStatus.CANCELLED.value}:
            self.brain.cancel(lambda event, state, step: self.brain_signals.update.emit(event, state, step))
        if self.chat_thread:
            if self.chat_cancel_event is not None:
                self.chat_cancel_event.set()
                ollama_manager.cancel_generation()
            self.chat_thread.requestInterruption()
            self.loading.setText("Stopping generation...")
            self.stop_button.setEnabled(False)

    def clear_chat(self) -> None:
        self.history.clear()
        self.conversation.clear()

    def handle_pending_action(self) -> None:
        pending = action_manager.describe()
        if not pending:
            return
        dialog = ApprovalDialog(pending, self)
        result = action_manager.approve() if dialog.exec() == QDialog.DialogCode.Accepted else action_manager.deny()
        self.last_raw_response = result
        self.append_message("Martin AI", format_response(result, {"kind": "action"}), False)

    def refresh_live_status(self) -> None:
        if self.closing:
            return
        # Do not let a slow sensor or offline server accumulate polling threads.
        if not self.live_refresh_running:
            self.live_refresh_running = True
            self.run_background(
                self.collect_live_status,
                self.apply_live_status,
                self.live_status_failed,
            )
        if not self.crash_refresh_running and time.time() - crash_detective.last_poll >= 30:
            self.crash_refresh_running = True
            self.run_background(crash_detective.poll, self.apply_crash_poll, self.crash_poll_failed)
        game_active = bool(self.game_mode_engine.active and self.game_optimizer.ai_resource_mode_active)
        ollama_due = not game_active or time.monotonic() - self.last_ollama_game_check >= 30
        if not self.ollama_refresh_running and ollama_due:
            self.last_ollama_game_check = time.monotonic()
            self.ollama_refresh_running = True
            self.run_background(
                self.check_ollama,
                self.apply_ollama_status,
                self.ollama_status_failed,
            )

    def collect_live_status(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        game = live_game_status()
        if game:
            if time.monotonic() - self.last_game_network_probe >= 60:
                self.last_game_network_probe = time.monotonic()
                self.game_network_cache = measure_network_jitter()
                self.game_network_cache["available"] = bool(self.game_network_cache.get("available"))
            game["network"] = self.game_network_cache
        return game, game if game else system_sample()

    @Slot(object)
    def apply_live_status(self, payload: object) -> None:
        self.live_refresh_running = False
        game, sample = payload  # type: ignore[misc]
        self.last_live_sample = sample
        prior_game = self.last_game
        game_events = self.game_mode_engine.update(game, sample)
        optimizer_status = self.game_optimizer.get_optimization_status()
        if game and self.game_mode_engine.active:
            optimizer_status = (self.game_optimizer.monitor_game_performance(game, sample)
                                if optimizer_status.get("active") else self.game_optimizer.start_game_session(game, sample))
        elif optimizer_status.get("active"):
            optimizer_status = self.game_optimizer.restore_session_changes("game process exited")
        for optimizer_event in self.game_optimizer.pop_notifications():
            self.add_notification("Game Optimization", optimizer_event["message"], "Informational",
                                  f"optimizer:{optimizer_event['key']}:{optimizer_event['message']}")
        for event in game_events:
            if event["type"] == "game_crashed":
                result = crash_detective.investigate(str(event.get("game") or "Game"), str(event.get("executable") or ""),
                                                     events=event.get("relevant_errors", []), telemetry=event.get("summary", {}))
                incident = result.get("incident", {})
                event["message"] = result.get("message", event["message"])
                self.refresh_crash_history()
                self.add_notification("Crash Detective", event["message"], "High" if incident.get("confidence") == "High" else "Medium",
                                      f"crash:{incident.get('application')}:{incident.get('cause')}")
            severity = "High" if event["type"] == "game_crashed" else ("Medium" if event["type"] == "performance_warning" else "Informational")
            if event["type"] in {"game_crashed", "performance_warning", "game_started", "game_switched", "game_ended"}:
                self.add_notification("Game Mode", event["message"], severity, f"game:{event['type']}:{event.get('game')}:{event.get('key', '')}")
            if event.get("report_path"): self.refresh_reports()
        game = game or (prior_game if self.game_mode_engine.active else None)
        self.last_game = game
        for alert in alert_manager.update_system_sample(sample):
            if self.add_notification(alert.title, alert.message, alert.severity, alert.key):
                alert_manager.mark_shown(alert.key)

        ram = float(sample.get("ram_percent", 0))
        gpu = sample.get("gpu", {})
        self.top_ram.setText(f"RAM: {ram:.0f}%")
        self.top_gpu.setText(f"GPU: {gpu.get('temperature_c', 0):.0f} C" if gpu.get("available") else "GPU: N/A")
        self.monitor_indicator.setText("● Monitor: active" if self.monitor_process and self.monitor_process.state() != QProcess.ProcessState.NotRunning else "● Monitor: available")
        if not game:
            self.top_game.setText("Game Mode: INACTIVE")
            self.game_name.setText("No supported game is currently running.")
            self.game_details.setText("Executable: --   |   Elapsed: --")
            self.game_path.setText("Installation Path: --")
            self.game_detection_details.setText("Detection: Automatic | Confidence: -- | Status: Inactive")
            self.game_network.setText("Network: Unavailable")
            self.game_recent_event.setText(f"Recent event: {self.game_mode_engine.last_event}")
            self.game_optimization.setText(f"Optimization: {str(optimizer_status.get('status', 'Inactive')).upper()} | Profile: {optimizer_status.get('mode', 'Balanced')} | Bottleneck: {optimizer_status.get('bottleneck', 'None')}")
            for card in (self.metric_cpu, self.metric_ram, self.metric_gpu, self.metric_vram):
                card.update_value(0, "--")
            self.game_warnings.setText("No active warnings.")
            return
        verified_changes = optimizer_status.get("verified_changes", [])
        self.top_game.setText(f"Game Mode: ACTIVE — {game['game']}" + (" | OPTIMIZED" if verified_changes else " | MONITORING"))
        elapsed = int(game.get("elapsed_seconds", 0))
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.game_name.setText(str(game["game"]))
        self.game_details.setText(f"Executable: {game['executable']} | PID: {game.get('pid', '--')} | Launcher: {game.get('launcher') or 'Standalone'} | Elapsed: {hours:02d}:{minutes:02d}:{seconds:02d}")
        self.game_path.setText(f"Installation Path: {game.get('install_dir') or game.get('path') or 'Unavailable'}")
        self.game_detection_details.setText(f"Detection: {game.get('detection', 'Automatic')} | Confidence: {game.get('confidence_label', 'Unavailable')} ({game.get('confidence', 0)}%) | Status: {self.game_mode_engine.status}")
        network = game.get("network", {})
        self.game_network.setText(f"Network: {network.get('latency_ms', 'Unavailable')} ms | Loss: {network.get('packet_loss_percent', 'Unavailable')}%")
        self.game_recent_event.setText(f"Recent event: {self.game_mode_engine.last_event}")
        failed_changes = optimizer_status.get("failed_changes", [])
        applied = "\n".join(f"✓ {row['action']}: {row['previous_value']} -> {row['actual_value']}" for row in verified_changes) or "No verified system changes."
        failed = "\n".join(f"✗ {row['action']}: {row.get('error') or 'verification failed'}" for row in failed_changes)
        telemetry = optimizer_status.get("telemetry", {})
        self.game_optimization.setText(
            f"OPTIMIZATION\nProfile: {optimizer_status.get('mode', 'Balanced')} | Status: {str(optimizer_status.get('status', 'Inactive')).upper()} | "
            f"Bottleneck: {optimizer_status.get('bottleneck', 'None detected')} | Verified Changes: {len(verified_changes)}\n"
            f"Available RAM: {telemetry.get('available_ram_mb', 'Unavailable')} MB\n\nAPPLIED CHANGES\n{applied}" +
            (f"\n\nFAILED ACTIONS\n{failed}" if failed else ""))
        cpu_temp = game.get("cpu_temperature_c")
        process_cpu = float(game.get("game_process_cpu_percent", 0))
        cpu_text = f"Game {process_cpu:.0f}% | System {game['cpu_percent']:.0f}% / {cpu_temp:.0f} C" if cpu_temp is not None else f"Game {process_cpu:.0f}% | System {game['cpu_percent']:.0f}%"
        self.metric_cpu.update_value(float(game["cpu_percent"]), cpu_text)
        self.metric_ram.update_value(float(game["ram_percent"]))
        game_gpu = game.get("gpu", {})
        if game_gpu.get("available"):
            self.metric_gpu.update_value(float(game_gpu["utilization_percent"]), f"{game_gpu['utilization_percent']:.0f}% / {game_gpu['temperature_c']:.0f} C")
            total = float(game_gpu.get("memory_total_mb", 0))
            used = float(game_gpu.get("memory_used_mb", 0))
            self.metric_vram.update_value((used / total * 100) if total else 0, f"{used:.0f} / {total:.0f} MB")
        warning_text = warnings_for(game)
        self.game_warnings.setText("Warnings: " + " | ".join(warning_text) if warning_text else "No active warnings.")

    @Slot(object)
    def apply_crash_poll(self, payload: object) -> None:
        self.crash_refresh_running = False
        results = payload if isinstance(payload, list) else []
        for result in results:
            incident = result.get("incident", {})
            self.add_notification("Crash Detective", result.get("message", "An application crashed."),
                                  "High" if incident.get("confidence") == "High" else "Medium",
                                  f"crash:{incident.get('application')}:{incident.get('cause')}")
        if results: self.refresh_crash_history()

    def crash_poll_failed(self, _message: str) -> None:
        self.crash_refresh_running = False

    def check_ollama(self) -> OllamaState:
        ollama_manager.configure(base_url=str(self.settings["ollama_url"]), model=str(self.settings["model"]))
        state = ollama_manager.check(auto_start=False)
        if state.status in {"connected", "no_models"}:
            self.ollama_repair_attempts = 0
            return state
        repair_cooldown = min(300.0, 30.0 * (2 ** self.ollama_repair_attempts))
        if (state.status == "offline" and runtime_control.autonomy_enabled and self_repair_engine.enabled
                and self_repair_engine.pending is None and self.ollama_repair_attempts < 3
                and time.monotonic() - self.last_ollama_repair >= repair_cooldown):
            self.last_ollama_repair = time.monotonic()
            self.ollama_repair_attempts += 1
            result = self_repair_engine.prepare(
                "ollama.reconnect", "Ollama stopped or its API became unavailable",
                {"ollama_manager": ollama_manager}, ["Ollama process", "API availability", "configured model"],
                autonomous=True,
            )
            if result.get("success"):
                state = ollama_manager.check(auto_start=False, force=True)
        return state

    @Slot(object)
    def apply_ollama_status(self, online: object) -> None:
        self.ollama_refresh_running = False
        state = online if isinstance(online, OllamaState) else OllamaState()
        label = {"connected": "Ollama: Connected", "reconnecting": "Ollama: Reconnecting...",
                 "starting": "Ollama: Starting...", "no_models": "Ollama: Connected",
                 "offline": "Ollama: Offline", "error": "Ollama: Error"}.get(state.status, "Ollama: Offline")
        self.ollama_indicator.setText("● " + label)
        if state.status == "offline" and self_repair_engine.pending:
            self.add_notification("Ollama repair needs approval",
                                  "Ollama is unavailable. A safe reconnect plan is ready and needs your permission.",
                                  "Medium", "self-repair:ollama-pending")
        if state.selected_model and not self.settings.get("model"):
            self.settings["model"] = state.selected_model

    @Slot(str)
    def live_status_failed(self, error: str) -> None:
        self.live_refresh_running = False
        self.show_error(error)

    @Slot(str)
    def ollama_status_failed(self, _error: str) -> None:
        self.ollama_refresh_running = False
        self.apply_ollama_status(ollama_manager.state)

    def start_monitor(self) -> None:
        if self.monitor_process and self.monitor_process.state() != QProcess.ProcessState.NotRunning:
            return
        process = QProcess(self)
        process.setProgram(sys.executable)
        process.setArguments(["-u", "-m", "martin_ai.game_monitor"])
        process.setWorkingDirectory(str(Path(__file__).resolve().parent.parent))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.errorOccurred.connect(lambda _error: self.show_error(process.errorString()))
        process.start()
        self.monitor_process = process

    def stop_monitor(self) -> None:
        if self.monitor_process and self.monitor_process.state() != QProcess.ProcessState.NotRunning:
            self.monitor_process.terminate()
            if not self.monitor_process.waitForFinished(2000):
                self.monitor_process.kill()
                self.monitor_process.waitForFinished(1000)
        self.monitor_process = None
        self.monitor_indicator.setText("● Monitor: stopped")

    def refresh_system(self) -> None:
        self.system_output.setPlainText("Collecting system diagnostics…")
        def collect() -> dict[str, Any]:
            return {
                "system_summary": READ_ONLY_TOOLS["system_summary"](),
                "top_processes": READ_ONLY_TOOLS["top_processes"](limit=10),
                "nvidia_status": READ_ONLY_TOOLS["nvidia_status"](),
                "recent_windows_errors": READ_ONLY_TOOLS["recent_windows_errors"](log_name="Application", hours=24, limit=20),
            }
        self.run_background(collect, lambda result: self.system_output.setPlainText(json.dumps(result, indent=2, default=str)))

    def save_diagnostic_report(self) -> None:
        self.run_background(READ_ONLY_TOOLS["save_diagnostic_report"], lambda result: QMessageBox.information(self, "Diagnostic report", json.dumps(result, indent=2)))

    def refresh_reports(self) -> None:
        self.report_list.clear()
        paths = []
        for folder in (GAME_REPORT_DIR, DIAGNOSTIC_REPORT_DIR, INVESTIGATION_REPORT_DIR, SECURITY_REPORT_DIR):
            if folder.exists():
                paths.extend(path for path in folder.iterdir() if path.is_file())
        for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
            self.report_list.addItem(str(path))

    def refresh_incident_history(self) -> None:
        if not hasattr(self, "incident_list"):
            return
        self.incident_list.clear()
        for item in memory_manager.incident_history(50):
            self.incident_list.addItem(
                f"{item['created_at']} | {item.get('application') or 'System'} | "
                f"{item['problem']} | {item['status']}"
            )
        if not self.incident_list.count():
            self.incident_list.addItem("No incidents recorded yet.")

    def toggle_autonomy(self) -> None:
        runtime_control.set_autonomy(not runtime_control.autonomy_enabled)
        self.autonomy_button.setText("AUTONOMY ON" if runtime_control.autonomy_enabled else "AUTONOMY OFF")
        self.ai_state.setText("AI: MONITORING" if runtime_control.autonomy_enabled else "AI: PASSIVE ONLY")

    def emergency_stop(self) -> None:
        if runtime_control.emergency_stopped:
            runtime_control.resume()
            self.emergency_button.setText("STOP AI")
            self.ai_state.setText("AI: MONITORING")
            return
        runtime_control.stop()
        self.disable_remote_input()
        if action_manager.has_pending():
            action_manager.deny()
        self_repair_engine.cancel_pending()
        if self.brain:
            self.cancel_investigation()
        self.emergency_button.setText("RESUME AI")
        self.ai_state.setText("AI: STOPPED — PASSIVE MONITORING")

    def selected_report(self) -> Path | None:
        item = self.report_list.currentItem()
        return Path(item.text()) if item else None

    def open_selected_report(self) -> None:
        path = self.selected_report()
        if path and path.exists():
            os.startfile(path)

    def open_selected_folder(self) -> None:
        path = self.selected_report()
        if path:
            self.open_folder(path.parent)

    def open_folder(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def store_settings(self) -> None:
        self.settings = {
            "model": self.model_input.text().strip() or DEFAULTS["model"],
            "ollama_url": self.url_input.text().strip() or DEFAULTS["ollama_url"],
            "poll_interval": self.poll_input.value(),
            "auto_monitor": self.auto_monitor_input.isChecked(),
            "minimize_to_tray": self.tray_input.isChecked(),
            "always_on_top": self.always_input.isChecked(),
        }
        try:
            save_settings(self.settings)
            requested_level = self.permission_input.currentIndex() + 1
            permission_manager.set_level(requested_level, user_authorized=True)
            runtime_control.set_simulation(self.simulation_input.isChecked())
            self.agent_settings.update({
                "simulation_mode": runtime_control.simulation_mode,
                "autonomy_enabled": runtime_control.autonomy_enabled,
            "permission_level": int(permission_manager.current_level),
                "game_mode": {
                    "automatic_detection": self.game_detection_input.isChecked(),
                    "auto_enable": self.game_auto_enable_input.isChecked(),
                    "performance_monitoring": self.game_performance_input.isChecked(),
                    "crash_detection": self.game_crash_input.isChecked(),
                    "session_reports": self.game_reports_input.isChecked(),
                    "performance_notifications": self.game_notifications_input.isChecked(),
                    "optimization_level": self.game_optimization_level_input.currentText(),
                    "ai_resource_mode": self.game_ai_resource_input.isChecked(),
                    "allow_idle_model_unload": self.game_idle_unload_input.isChecked(),
                },
            })
            self.game_mode_engine.configure(self.agent_settings["game_mode"])
            self.game_optimizer.configure(self.agent_settings["game_mode"])
            save_agent_settings(self.agent_settings)
            self.permission_badge.setText(f"LEVEL {int(permission_manager.current_level)}")
            self.poll_timer.setInterval(int(self.settings["poll_interval"]) * 1000)
            self.top_model.setText(f"Model: {self.settings['model']}")
            self._apply_window_flags()
            if self.settings["minimize_to_tray"] and QSystemTrayIcon.isSystemTrayAvailable():
                self.tray_icon.show()
            else:
                self.tray_icon.hide()
            QMessageBox.information(self, "Settings", "Settings saved.")
        except OSError as exc:
            self.show_error(str(exc))

    def _apply_window_flags(self) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(self.settings["always_on_top"]))
        if self.isVisible():
            self.show()

    @Slot(str)
    def show_error(self, message: str) -> None:
        logging.error(message)
        if not self.closing:
            QMessageBox.critical(self, "Martin AI error", message)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.settings.get("minimize_to_tray") and QSystemTrayIcon.isSystemTrayAvailable() and not self.force_quit:
            self.hide()
            self.tray_icon.showMessage("Martin AI", "Martin AI is still running in the system tray.")
            event.ignore()
            return
        self.closing = True
        self.poll_timer.stop()
        self.stop_monitor()
        for thread in list(self.threads):
            thread.requestInterruption()
            thread.quit()
        for thread in list(self.threads):
            thread.wait(5000)
        self.game_optimizer.restore_session_changes("Martin AI closed")
        self.phone_remote.stop()
        unload_model(str(self.settings["ollama_url"]).rstrip("/"), str(self.settings["model"]))
        memory_manager.db.close()
        event.accept()


def create_application(argv: list[str] | None = None) -> QApplication:
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
        except (AttributeError, OSError):
            pass
    app = QApplication.instance() or QApplication(argv or sys.argv)
    app.setApplicationName("Martin AI")
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    app.setStyle("Fusion")
    return app


def run() -> int:
    app = create_application()
    window = MartinMainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())

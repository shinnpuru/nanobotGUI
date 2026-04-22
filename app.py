"""PySide6 GUI application for nanobot."""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import platform
import plistlib
import queue
import shlex
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QAction, QCloseEvent, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QListWidget,
)

from nanobot.cli.commands import sync_workspace_templates
from nanobot.config.loader import get_config_path, load_config, save_config
from nanobot.config.schema import ChannelsConfig, Config, MCPServerConfig
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule
from nanobot.providers.registry import PROVIDERS
from nanobot.config.paths import get_data_dir, get_workspace_path


APP_STYLE = """
QMainWindow {
    background: #0f131a;
}
QWidget {
    color: #e6edf3;
    font-size: 13px;
}
QLabel#TitleLabel {
    font-size: 14px;
    font-weight: 700;
    color: #f0f6fc;
}
QLabel#SubTitleLabel {
    color: #9aa7b8;
    font-size: 12px;
}
QStatusBar {
    background: #0d1117;
    border-top: 1px solid #283241;
    color: #a8b7c9;
}
QGroupBox {
    border: 1px solid #2d3748;
    border-radius: 10px;
    margin-top: 10px;
    padding: 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
}
QPushButton {
    background: #1f6feb;
    border: none;
    border-radius: 8px;
    padding: 7px 12px;
    font-weight: 600;
    color: white;
}
QPushButton:hover {
    background: #2f81f7;
}
QPushButton:pressed {
    background: #1a62d4;
}
QPushButton:disabled {
    background: #3a4657;
    color: #9aa7b8;
}
QLineEdit, QPlainTextEdit, QSpinBox, QComboBox, QListWidget, QTableWidget {
    background: #0d1117;
    border: 1px solid #2d3748;
    border-radius: 8px;
    padding: 5px;
}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #2f81f7;
}
QTableWidget {
    gridline-color: #243040;
    selection-background-color: #1f6feb;
    alternate-background-color: #0f1621;
}
QHeaderView::section {
    background: #151b23;
    color: #c7d4e3;
    border: none;
    border-right: 1px solid #2a3545;
    border-bottom: 1px solid #2a3545;
    padding: 6px;
    font-weight: 600;
}
QListWidget {
    padding: 6px;
}
QListWidget::item {
    padding: 10px 8px;
    border-radius: 8px;
    margin: 2px 0;
}
QListWidget::item:selected {
    background: #1f6feb;
    color: #ffffff;
}
QListWidget::item:hover {
    background: #1a2330;
}
QSplitter::handle {
    background: #202938;
    width: 1px;
}
QScrollBar:vertical {
    background: #0d1117;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #304057;
    min-height: 20px;
    border-radius: 5px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
"""


def _now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _json_pretty(data: object) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _gui_settings_path() -> Path:
    return get_data_dir() / "gui" / "settings.json"


def _load_gui_settings() -> dict[str, object]:
    path = _gui_settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_gui_settings(settings: dict[str, object]) -> None:
    path = _gui_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_pretty(settings), encoding="utf-8")


class _QueueStream:
    def __init__(self, log_queue: mp.Queue[str]) -> None:
        self.log_queue = log_queue
        self._buffer = ""

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self.log_queue.put(line.rstrip())
        return len(data)

    def flush(self) -> None:
        if self._buffer.strip():
            self.log_queue.put(self._buffer.rstrip())
        self._buffer = ""


def _run_gateway_process(port: int, log_queue: mp.Queue[str]) -> None:
    stream = _QueueStream(log_queue)
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = stream
    sys.stderr = stream
    try:
        from nanobot.cli.commands import gateway

        gateway(port=port, workspace=None, verbose=False, config=None)
    except Exception as exc:
        log_queue.put(f"Gateway process error: {exc}")
        raise
    finally:
        stream.flush()
        sys.stdout = old_stdout
        sys.stderr = old_stderr


class ConfigStore:
    def __init__(self) -> None:
        self.config = load_config()

    def reload(self) -> Config:
        self.config = load_config()
        return self.config

    def save(self) -> None:
        save_config(self.config)


def run_auto_onboard() -> list[str]:
    logs: list[str] = []
    config_path = get_config_path()
    if config_path.exists():
        config = load_config()
        save_config(config)
        logs.append(f"Config refreshed: {config_path}")
    else:
        config = Config()
        save_config(config)
        logs.append(f"Config created: {config_path}")

    workspace = get_workspace_path(config.agents.defaults.workspace)
    sync_workspace_templates(workspace)
    logs.append(f"Workspace ready: {workspace}")
    return logs


class AutoStartManager:
    LAUNCH_AGENT_LABEL = "io.nanobot.gui"
    WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    WINDOWS_VALUE_NAME = "nanobot-gui"
    LINUX_DESKTOP_FILE = "nanobot-gui.desktop"

    @classmethod
    def is_supported(cls) -> bool:
        return platform.system() in {"Darwin", "Windows", "Linux"}

    @classmethod
    def _platform(cls) -> str:
        return platform.system()

    @classmethod
    def _working_directory(cls) -> Path:
        return Path(__file__).resolve().parents[2]

    @classmethod
    def _program_arguments(cls) -> list[str]:
        if getattr(sys, "frozen", False):
            return [str(Path(sys.executable).resolve())]
        return [str(Path(sys.executable).resolve()), "-m", "nanobot.gui"]

    @classmethod
    def _command_line(cls) -> str:
        return " ".join(shlex.quote(part) for part in cls._program_arguments())

    @classmethod
    def _plist_path(cls) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{cls.LAUNCH_AGENT_LABEL}.plist"

    @classmethod
    def _linux_autostart_path(cls) -> Path:
        xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
        config_home = Path(xdg_config_home).expanduser() if xdg_config_home else (Path.home() / ".config")
        return config_home / "autostart" / cls.LINUX_DESKTOP_FILE

    @classmethod
    def _build_plist_content(cls) -> dict:
        log_dir = get_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return {
            "Label": cls.LAUNCH_AGENT_LABEL,
            "ProgramArguments": cls._program_arguments(),
            "RunAtLoad": True,
            "KeepAlive": False,
            "WorkingDirectory": str(cls._working_directory()),
            "StandardOutPath": str(log_dir / "gui-autostart.log"),
            "StandardErrorPath": str(log_dir / "gui-autostart.log"),
        }

    @classmethod
    def _write_plist(cls) -> Path:
        plist_path = cls._plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        with open(plist_path, "wb") as f:
            plistlib.dump(cls._build_plist_content(), f)
        return plist_path

    @classmethod
    def _bootout(cls) -> None:
        plist_path = cls._plist_path()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True,
            text=True,
            check=False,
        )

    @classmethod
    def _bootstrap(cls, plist_path: Path) -> None:
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "launchctl bootstrap failed")

    @classmethod
    def _set_enabled_macos(cls, enabled: bool) -> None:
        plist_path = cls._plist_path()
        if enabled:
            plist_path = cls._write_plist()
            cls._bootout()
            cls._bootstrap(plist_path)
            return

        cls._bootout()
        if plist_path.exists():
            plist_path.unlink()

    @classmethod
    def _is_enabled_macos(cls) -> bool:
        return cls._plist_path().exists()

    @classmethod
    def _set_enabled_windows(cls, enabled: bool) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cls.WINDOWS_RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, cls.WINDOWS_VALUE_NAME, 0, winreg.REG_SZ, cls._command_line())
            else:
                try:
                    winreg.DeleteValue(key, cls.WINDOWS_VALUE_NAME)
                except FileNotFoundError:
                    pass

    @classmethod
    def _is_enabled_windows(cls) -> bool:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.WINDOWS_RUN_KEY, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, cls.WINDOWS_VALUE_NAME)
                return bool(value)
        except FileNotFoundError:
            return False

    @classmethod
    def _linux_desktop_entry(cls) -> str:
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Version=1.0\n"
            "Name=nanobot GUI\n"
            "Comment=Start nanobot GUI on login\n"
            f"Exec={cls._command_line()}\n"
            f"Path={str(cls._working_directory())}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        )

    @classmethod
    def _set_enabled_linux(cls, enabled: bool) -> None:
        desktop_path = cls._linux_autostart_path()
        if enabled:
            desktop_path.parent.mkdir(parents=True, exist_ok=True)
            desktop_path.write_text(cls._linux_desktop_entry(), encoding="utf-8")
            return

        if desktop_path.exists():
            desktop_path.unlink()

    @classmethod
    def _is_enabled_linux(cls) -> bool:
        return cls._linux_autostart_path().exists()

    @classmethod
    def is_enabled(cls) -> bool:
        if not cls.is_supported():
            return False

        current = cls._platform()
        if current == "Darwin":
            return cls._is_enabled_macos()
        if current == "Windows":
            return cls._is_enabled_windows()
        if current == "Linux":
            return cls._is_enabled_linux()
        return False

    @classmethod
    def set_enabled(cls, enabled: bool) -> None:
        if not cls.is_supported():
            raise RuntimeError("Auto-start is not supported on this OS.")

        current = cls._platform()
        if current == "Darwin":
            cls._set_enabled_macos(enabled)
            return
        if current == "Windows":
            cls._set_enabled_windows(enabled)
            return
        if current == "Linux":
            cls._set_enabled_linux(enabled)
            return

        raise RuntimeError("Auto-start is not supported on this OS.")


class GatewayPanel(QWidget):
    def __init__(self, store: ConfigStore) -> None:
        super().__init__()
        self.store = store
        self.process: mp.Process | None = None
        self.log_queue: mp.Queue[str] | None = None
        self.log_timer = QTimer(self)
        self.log_timer.setInterval(200)
        self.log_timer.timeout.connect(self._poll_process_output)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        controls = QHBoxLayout()
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(self.store.config.gateway.port)
        self.start_btn = QPushButton("Start Gateway")
        self.stop_btn = QPushButton("Stop Gateway")
        self.stop_btn.setEnabled(False)
        self.clear_btn = QPushButton("Clear Logs")
        self.status = QLabel("Status: Stopped")

        controls.addWidget(QLabel("Port"))
        controls.addWidget(self.port)
        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.clear_btn)
        controls.addStretch()
        controls.addWidget(self.status)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Menlo", 11))
        self.log.setPlaceholderText("Gateway logs will appear here...")

        root.addLayout(controls)
        root.addWidget(self.log)

        self.start_btn.clicked.connect(self.start_gateway)
        self.stop_btn.clicked.connect(self.stop_gateway)
        self.clear_btn.clicked.connect(self.log.clear)

    def append_log(self, text: str) -> None:
        self.log.appendPlainText(f"[{_now_text()}] {text}")

    def start_gateway(self) -> None:
        if self.process and self.process.is_alive():
            return

        self.store.config.gateway.port = self.port.value()
        self.store.save()

        self.log_queue = mp.Queue()
        self.process = mp.Process(
            target=_run_gateway_process,
            args=(self.port.value(), self.log_queue),
            daemon=True,
        )
        self.process.start()
        self.log_timer.start()

        self.append_log(
            f"Gateway started in subprocess pid={self.process.pid}, port={self.port.value()}"
        )

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Status: Running")

    def stop_gateway(self) -> None:
        if not self.process:
            return
        self.append_log("Stopping Gateway...")
        if self.process.is_alive() and self.process.pid:
            try:
                os.kill(self.process.pid, signal.SIGINT)
            except Exception:
                pass

        self.process.join(timeout=3)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=2)

        self._poll_process_output()
        self._on_finished()

    def _poll_process_output(self) -> None:
        if self.log_queue:
            while True:
                try:
                    data = self.log_queue.get_nowait()
                except queue.Empty:
                    break
                if data and str(data).strip():
                    self.append_log(str(data).rstrip())

        if self.process and not self.process.is_alive():
            self._on_finished()

    def _on_finished(self) -> None:
        self.log_timer.stop()
        exit_code = self.process.exitcode if self.process else None
        self.append_log(f"Gateway exited, exit_code={exit_code}")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText("Status: Stopped")
        self.process = None
        self.log_queue = None

    def shutdown(self) -> None:
        if self.process and self.process.is_alive():
            self.stop_gateway()
        self.log_timer.stop()
        self.process = None
        self.log_queue = None


class SkillsPanel(QWidget):
    def __init__(self, store: ConfigStore) -> None:
        super().__init__()
        self.store = store
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        top = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.new_skill_name = QLineEdit()
        self.new_skill_name.setPlaceholderText("New skill directory name")
        self.create_btn = QPushButton("Create in workspace/skills")
        top.addWidget(self.refresh_btn)
        top.addStretch()
        top.addWidget(self.new_skill_name)
        top.addWidget(self.create_btn)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Skill", "Source", "Path"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)

        root.addLayout(top)
        root.addWidget(self.table)

        self.refresh_btn.clicked.connect(self.refresh)
        self.create_btn.clicked.connect(self.create_skill)
        self.refresh()

    def _rows(self) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        builtin_root = Path(__file__).resolve().parents[1] / "skills"
        workspace_root = get_workspace_path(self.store.config.agents.defaults.workspace) / "skills"

        for source, root in (("builtin", builtin_root), ("workspace", workspace_root)):
            if not root.exists():
                continue
            for item in sorted(root.iterdir()):
                if not item.is_dir():
                    continue
                if (item / "SKILL.md").exists() or source == "workspace":
                    rows.append((item.name, source, str(item)))
        return rows

    def refresh(self) -> None:
        rows = self._rows()
        self.table.setRowCount(len(rows))
        for idx, (name, source, path) in enumerate(rows):
            self.table.setItem(idx, 0, QTableWidgetItem(name))
            self.table.setItem(idx, 1, QTableWidgetItem(source))
            self.table.setItem(idx, 2, QTableWidgetItem(path))

    def create_skill(self) -> None:
        name = self.new_skill_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Notice", "Please enter a skill directory name")
            return
        target = get_workspace_path(self.store.config.agents.defaults.workspace) / "skills" / name
        target.mkdir(parents=True, exist_ok=True)
        skill_md = target / "SKILL.md"
        if not skill_md.exists():
            skill_md.write_text("# New Skill\n\nDescribe your skill here.\n", encoding="utf-8")
        self.new_skill_name.clear()
        self.refresh()


class ProvidersPanel(QWidget):
    def __init__(self, store: ConfigStore) -> None:
        super().__init__()
        self.store = store
        self.key_edits: dict[str, QLineEdit] = {}
        self.base_edits: dict[str, QLineEdit] = {}
        self.oauth_processes: list[subprocess.Popen] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        top_row = QHBoxLayout()
        model_box = QGroupBox("Default Model")
        model_layout = QHBoxLayout(model_box)
        self.model_edit = QLineEdit(self.store.config.agents.defaults.model)
        model_layout.addWidget(self.model_edit)

        self.save_btn = QPushButton("Save All Providers")
        top_row.addWidget(model_box, 1)
        top_row.addWidget(self.save_btn)
        root.addLayout(top_row)

        table = QTableWidget(len(PROVIDERS), 4)
        table.setHorizontalHeaderLabels(["Provider", "apiKey", "apiBase", "OAuth"])
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table = table
        root.addWidget(table)

        self._build_rows()
        self.save_btn.clicked.connect(self.save)

    def _build_rows(self) -> None:
        for row, spec in enumerate(PROVIDERS):
            provider_name = spec.name
            cfg = getattr(self.store.config.providers, provider_name)

            name_item = QTableWidgetItem(provider_name.replace("_", "-"))
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 0, name_item)

            key_edit = QLineEdit(cfg.api_key or "")
            key_edit.setEchoMode(QLineEdit.Password)
            base_edit = QLineEdit(cfg.api_base or "")
            self.key_edits[provider_name] = key_edit
            self.base_edits[provider_name] = base_edit

            self.table.setCellWidget(row, 1, key_edit)
            self.table.setCellWidget(row, 2, base_edit)

            if spec.is_oauth:
                btn = QPushButton("Login")
                btn.clicked.connect(lambda _checked=False, n=provider_name: self.login_oauth(n))
                self.table.setCellWidget(row, 3, btn)
            else:
                self.table.setItem(row, 3, QTableWidgetItem("-"))

    def login_oauth(self, provider_name: str) -> None:
        arg = provider_name.replace("_", "-")
        proc = subprocess.Popen(
            [sys.executable, "-m", "nanobot", "provider", "login", arg],
            start_new_session=True,
        )
        self.oauth_processes.append(proc)
        QMessageBox.information(self, "OAuth", f"Login flow started: {arg}")

    def save(self) -> None:
        self.store.config.agents.defaults.model = self.model_edit.text().strip()
        for spec in PROVIDERS:
            provider_name = spec.name
            cfg = getattr(self.store.config.providers, provider_name)
            cfg.api_key = self.key_edits[provider_name].text().strip()
            base = self.base_edits[provider_name].text().strip()
            cfg.api_base = base or None
        self.store.save()
        QMessageBox.information(self, "Saved", "Providers configuration saved")

    def shutdown(self) -> None:
        for proc in self.oauth_processes:
            if proc.poll() is not None:
                continue
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except Exception:
                continue

            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except Exception:
                    pass

        self.oauth_processes = []


class ChannelsPanel(QWidget):
    def __init__(self, store: ConfigStore) -> None:
        super().__init__()
        self.store = store
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        top = QHBoxLayout()
        self.reload_btn = QPushButton("Reload")
        self.save_btn = QPushButton("Save JSON")
        self.format_btn = QPushButton("Format JSON")
        top.addWidget(QLabel("Channels JSON Editor"))
        top.addStretch()
        top.addWidget(self.reload_btn)
        top.addWidget(self.format_btn)
        top.addWidget(self.save_btn)

        editor_group = QGroupBox("channels")
        editor_layout = QVBoxLayout(editor_group)
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Menlo", 11))
        self.editor.setPlaceholderText("Edit channels config as JSON...")
        editor_layout.addWidget(self.editor)

        root.addLayout(top)
        root.addWidget(editor_group)

        self.reload_btn.clicked.connect(self.reload)
        self.format_btn.clicked.connect(self.format_json)
        self.save_btn.clicked.connect(self.save)
        self.reload()

    def reload(self) -> None:
        channels = self.store.config.channels.model_dump(by_alias=True)
        self.editor.setPlainText(_json_pretty(channels))

    def format_json(self) -> None:
        raw = self.editor.toPlainText().strip()
        if not raw:
            self.editor.setPlainText("{}")
            return
        try:
            parsed = json.loads(raw)
            self.editor.setPlainText(_json_pretty(parsed))
        except Exception as exc:
            QMessageBox.critical(self, "Invalid JSON", str(exc))

    def save(self) -> None:
        try:
            raw = self.editor.toPlainText().strip()
            channels = json.loads(raw) if raw else {}
            if not isinstance(channels, dict):
                QMessageBox.warning(self, "Notice", "Channels JSON must be an object")
                return

            self.store.config.channels = ChannelsConfig.model_validate(channels)
            self.store.save()
            self.reload()
            QMessageBox.information(self, "Saved", "Channels configuration saved")
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))


class MCPPanel(QWidget):
    def __init__(self, store: ConfigStore) -> None:
        super().__init__()
        self.store = store
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        bar = QHBoxLayout()
        self.add_btn = QPushButton("Add Row")
        self.reload_btn = QPushButton("Reload")
        self.save_btn = QPushButton("Save")
        bar.addWidget(self.add_btn)
        bar.addStretch()
        bar.addWidget(self.reload_btn)
        bar.addWidget(self.save_btn)
        root.addLayout(bar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["name", "command", "args", "env(JSON)", "url"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        root.addWidget(self.table)

        self.add_btn.clicked.connect(self.add_row)
        self.reload_btn.clicked.connect(self.reload)
        self.save_btn.clicked.connect(self.save)
        self.reload()

    def add_row(self) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col in range(5):
            self.table.setItem(row, col, QTableWidgetItem(""))

    def reload(self) -> None:
        servers = self.store.config.tools.mcp_servers
        self.table.setRowCount(0)
        for name, cfg in servers.items():
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(cfg.command or ""))
            self.table.setItem(row, 2, QTableWidgetItem(" ".join(cfg.args or [])))
            self.table.setItem(row, 3, QTableWidgetItem(_json_pretty(cfg.env or {})))
            self.table.setItem(row, 4, QTableWidgetItem(cfg.url or ""))

    def save(self) -> None:
        result: dict[str, MCPServerConfig] = {}
        try:
            for row in range(self.table.rowCount()):
                name_item = self.table.item(row, 0)
                if not name_item:
                    continue
                name = name_item.text().strip()
                if not name:
                    continue

                command = (self.table.item(row, 1).text().strip() if self.table.item(row, 1) else "")
                args_text = (self.table.item(row, 2).text().strip() if self.table.item(row, 2) else "")
                env_text = (self.table.item(row, 3).text().strip() if self.table.item(row, 3) else "{}")
                url = (self.table.item(row, 4).text().strip() if self.table.item(row, 4) else "")

                if args_text.startswith("["):
                    parsed_args = json.loads(args_text)
                    args = [str(v) for v in parsed_args]
                else:
                    args = shlex.split(args_text) if args_text else []

                env = json.loads(env_text) if env_text else {}
                if not isinstance(env, dict):
                    raise ValueError(f"{name} env must be a JSON object")

                result[name] = MCPServerConfig(command=command, args=args, env=env, url=url)

            self.store.config.tools.mcp_servers = result
            self.store.save()
            QMessageBox.information(self, "Saved", "MCP configuration saved")
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))


class CronPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.service = CronService(get_data_dir() / "cron" / "jobs.json")

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        actions = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh Jobs")
        self.run_selected_btn = QPushButton("Run Selected")
        self.remove_selected_btn = QPushButton("Remove Selected")
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.run_selected_btn)
        actions.addWidget(self.remove_selected_btn)
        actions.addStretch()
        root.addLayout(actions)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["ID", "Name", "Enabled", "Schedule", "Next Run", "Last Run", "Status"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        root.addWidget(self.table)

        job_actions_group = QGroupBox("Job Actions")
        job_actions = QGridLayout(job_actions_group)
        self.job_id = QLineEdit()
        self.enable_flag = QComboBox()
        self.enable_flag.addItems(["true", "false"])
        self.run_btn = QPushButton("Run")
        self.remove_btn = QPushButton("Remove")
        self.enable_btn = QPushButton("Enable")
        job_actions.addWidget(QLabel("Job ID"), 0, 0)
        job_actions.addWidget(self.job_id, 0, 1)
        job_actions.addWidget(self.run_btn, 0, 2)
        job_actions.addWidget(self.remove_btn, 0, 3)
        job_actions.addWidget(self.enable_flag, 0, 4)
        job_actions.addWidget(self.enable_btn, 0, 5)

        add_job_group = QGroupBox("Add Job")
        form = QGridLayout(add_job_group)
        self.add_name = QLineEdit()
        self.add_message = QLineEdit()
        self.add_every = QLineEdit()
        self.add_cron = QLineEdit()
        self.add_at = QLineEdit()
        self.add_tz = QLineEdit()
        self.add_channel = QLineEdit()
        self.add_to = QLineEdit()
        self.add_deliver = QCheckBox("Deliver")
        self.add_btn = QPushButton("Add")
        form.addWidget(QLabel("Name"), 0, 0)
        form.addWidget(self.add_name, 0, 1)
        form.addWidget(QLabel("Message"), 0, 2)
        form.addWidget(self.add_message, 0, 3, 1, 3)
        form.addWidget(QLabel("Every (s)"), 1, 0)
        form.addWidget(self.add_every, 1, 1)
        form.addWidget(QLabel("Cron"), 1, 2)
        form.addWidget(self.add_cron, 1, 3)
        form.addWidget(QLabel("At"), 1, 4)
        form.addWidget(self.add_at, 1, 5)
        form.addWidget(QLabel("TZ"), 2, 0)
        form.addWidget(self.add_tz, 2, 1)
        form.addWidget(QLabel("Channel"), 2, 2)
        form.addWidget(self.add_channel, 2, 3)
        form.addWidget(QLabel("To"), 2, 4)
        form.addWidget(self.add_to, 2, 5)
        form.addWidget(self.add_deliver, 3, 0)
        form.addWidget(self.add_btn, 3, 1)

        root.addWidget(job_actions_group)
        root.addWidget(add_job_group)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Menlo", 11))
        self.log.setPlaceholderText("Cron read/write logs will appear here...")
        root.addWidget(self.log)

        self.refresh_btn.clicked.connect(self.refresh_jobs)
        self.run_selected_btn.clicked.connect(self.run_selected)
        self.remove_selected_btn.clicked.connect(self.remove_selected)
        self.run_btn.clicked.connect(self.run_job)
        self.remove_btn.clicked.connect(self.remove_job)
        self.enable_btn.clicked.connect(self.enable_job)
        self.add_btn.clicked.connect(self.add_job)
        self.refresh_jobs()

    def append_log(self, text: str) -> None:
        self.log.appendPlainText(f"[{_now_text()}] {text}")

    def _format_ts(self, value: int | None) -> str:
        if not value:
            return "-"
        return datetime.fromtimestamp(value / 1000).strftime("%m-%d %H:%M")

    def _schedule_text(self, schedule: CronSchedule) -> str:
        if schedule.kind == "every":
            seconds = int((schedule.every_ms or 0) / 1000)
            return f"Every {seconds}s"
        if schedule.kind == "cron":
            tz = f" ({schedule.tz})" if schedule.tz else ""
            return f"Cron {schedule.expr or ''}{tz}"
        return f"at {self._format_ts(schedule.at_ms)}"

    def refresh_jobs(self) -> None:
        jobs = self.service.list_jobs(include_disabled=True)
        self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            self.table.setItem(row, 0, QTableWidgetItem(job.id))
            self.table.setItem(row, 1, QTableWidgetItem(job.name))
            self.table.setItem(row, 2, QTableWidgetItem("Yes" if job.enabled else "No"))
            self.table.setItem(row, 3, QTableWidgetItem(self._schedule_text(job.schedule)))
            self.table.setItem(row, 4, QTableWidgetItem(self._format_ts(job.state.next_run_at_ms)))
            self.table.setItem(row, 5, QTableWidgetItem(self._format_ts(job.state.last_run_at_ms)))
            self.table.setItem(row, 6, QTableWidgetItem(job.state.last_status or "-"))
        self.append_log(f"Loaded {len(jobs)} jobs")

    def _run_coro(self, coro):
        try:
            return asyncio.run(coro)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

    def _selected_job_id(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.text().strip() if item else None

    def run_selected(self) -> None:
        job_id = self._selected_job_id()
        if not job_id:
            QMessageBox.warning(self, "Notice", "Please select a job first")
            return
        ok = self._run_coro(self.service.run_job(job_id, force=True))
        self.append_log(f"Run job {job_id}: {'Success' if ok else 'Failed'}")
        self.refresh_jobs()

    def remove_selected(self) -> None:
        job_id = self._selected_job_id()
        if not job_id:
            QMessageBox.warning(self, "Notice", "Please select a job first")
            return
        ok = self.service.remove_job(job_id)
        self.append_log(f"Remove job {job_id}: {'Success' if ok else 'Failed'}")
        self.refresh_jobs()

    def run_job(self) -> None:
        job_id = self.job_id.text().strip()
        if job_id:
            ok = self._run_coro(self.service.run_job(job_id, force=True))
            self.append_log(f"Run job {job_id}: {'Success' if ok else 'Failed'}")
            self.refresh_jobs()

    def remove_job(self) -> None:
        job_id = self.job_id.text().strip()
        if job_id:
            ok = self.service.remove_job(job_id)
            self.append_log(f"Remove job {job_id}: {'Success' if ok else 'Failed'}")
            self.refresh_jobs()

    def enable_job(self) -> None:
        job_id = self.job_id.text().strip()
        flag = self.enable_flag.currentText()
        if job_id:
            enabled = flag == "true"
            job = self.service.enable_job(job_id, enabled=enabled)
            self.append_log(f"Set job {job_id} enabled={enabled}: {'success' if job else 'failed'}")
            self.refresh_jobs()

    def add_job(self) -> None:
        name = self.add_name.text().strip()
        message = self.add_message.text().strip()
        if not name or not message:
            QMessageBox.warning(self, "Notice", "add requires both name and message")
            return

        every = self.add_every.text().strip()
        cron_expr = self.add_cron.text().strip()
        at = self.add_at.text().strip()
        tz = self.add_tz.text().strip() or None

        try:
            delete_after_run = False
            if every:
                schedule = CronSchedule(kind="every", every_ms=int(float(every) * 1000))
            elif cron_expr:
                schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
            elif at:
                dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
                schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
                delete_after_run = True
            else:
                QMessageBox.warning(self, "Notice", "One of Every / Cron / At is required")
                return

            job = self.service.add_job(
                name=name,
                schedule=schedule,
                message=message,
                deliver=self.add_deliver.isChecked(),
                channel=self.add_channel.text().strip() or None,
                to=self.add_to.text().strip() or None,
                delete_after_run=delete_after_run,
            )
            self.append_log(f"Job added: {job.id} ({job.name})")
            self.refresh_jobs()
        except Exception as exc:
            QMessageBox.critical(self, "Add Failed", str(exc))


class AdvancedPanel(QWidget):
    def __init__(self, store: ConfigStore, main_window: "MainWindow") -> None:
        super().__init__()
        self.store = store
        self.main_window = main_window

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        self.workspace = QLineEdit(self.store.config.agents.defaults.workspace)
        self.workspace.setPlaceholderText("e.g. ~/.nanobot/workspace")
        self.max_iter = QSpinBox()
        self.max_iter.setRange(1, 200)
        self.max_iter.setValue(self.store.config.agents.defaults.max_tool_iterations)

        self.temperature = QLineEdit(str(self.store.config.agents.defaults.temperature))
        self.temperature.setPlaceholderText("0.0 - 2.0")

        self.restrict_ws = QCheckBox("Restrict tools to workspace directory only")
        self.restrict_ws.setChecked(self.store.config.tools.restrict_to_workspace)
        self.minimize_to_tray_on_close = QCheckBox("Close button minimizes to system tray")
        self.minimize_to_tray_on_close.setChecked(self.main_window.minimize_to_tray_on_close)
        self.autostart = QCheckBox("Start nanobot GUI at login")
        self.autostart_status = QLabel("")
        self.exec_timeout = QSpinBox()
        self.exec_timeout.setRange(1, 3600)
        self.exec_timeout.setValue(self.store.config.tools.exec.timeout)
        self.exec_timeout.setSuffix(" s")
        self.web_key = QLineEdit(self.store.config.tools.web.search.api_key)
        self.web_key.setEchoMode(QLineEdit.Password)
        self.web_key.setPlaceholderText("Brave Search API Key")
        self.web_max = QSpinBox()
        self.web_max.setRange(1, 50)
        self.web_max.setValue(self.store.config.tools.web.search.max_results)
        self.web_max.setSuffix(" items")

        agent_group = QGroupBox("Agent Settings")
        agent_grid = QGridLayout(agent_group)
        agent_grid.addWidget(QLabel("Workspace Path"), 0, 0)
        agent_grid.addWidget(self.workspace, 0, 1)
        agent_grid.addWidget(QLabel("Temperature"), 1, 0)
        agent_grid.addWidget(self.temperature, 1, 1)
        agent_grid.addWidget(QLabel("Max Tool Iterations"), 2, 0)
        agent_grid.addWidget(self.max_iter, 2, 1)

        security_group = QGroupBox("Security Settings")
        security_layout = QVBoxLayout(security_group)
        security_layout.addWidget(self.restrict_ws)
        security_layout.addWidget(self.minimize_to_tray_on_close)
        security_layout.addWidget(self.autostart)
        security_layout.addWidget(self.autostart_status)

        tool_group = QGroupBox("Tool Settings")
        tool_grid = QGridLayout(tool_group)
        tool_grid.addWidget(QLabel("Command Timeout"), 0, 0)
        tool_grid.addWidget(self.exec_timeout, 0, 1)
        tool_grid.addWidget(QLabel("Web Search API Key"), 1, 0)
        tool_grid.addWidget(self.web_key, 1, 1)
        tool_grid.addWidget(QLabel("Max Search Results"), 2, 0)
        tool_grid.addWidget(self.web_max, 2, 1)

        self.save_btn = QPushButton("Save Advanced Settings")
        root.addWidget(agent_group)
        root.addWidget(security_group)
        root.addWidget(tool_group)
        root.addWidget(self.save_btn)
        root.addStretch()

        self._refresh_autostart_status()
        self.save_btn.clicked.connect(self.save)

    def _refresh_autostart_status(self) -> None:
        supported = AutoStartManager.is_supported()
        self.autostart.setEnabled(supported)

        if not supported:
            self.autostart.setChecked(False)
            self.autostart_status.setText("Auto-start is not supported on this OS")
            return

        try:
            enabled = AutoStartManager.is_enabled()
            self.autostart.setChecked(enabled)
            self.autostart_status.setText(f"System auto-start: {'Enabled' if enabled else 'Disabled'}")
        except Exception as exc:
            self.autostart_status.setText(f"System auto-start: Unknown ({exc})")

    def save(self) -> None:
        try:
            self.store.config.agents.defaults.workspace = self.workspace.text().strip()
            self.store.config.agents.defaults.max_tool_iterations = self.max_iter.value()
            self.store.config.agents.defaults.temperature = float(self.temperature.text().strip())
            self.store.config.tools.restrict_to_workspace = self.restrict_ws.isChecked()
            self.store.config.tools.exec.timeout = self.exec_timeout.value()
            self.store.config.tools.web.search.api_key = self.web_key.text().strip()
            self.store.config.tools.web.search.max_results = self.web_max.value()
            self.store.save()
            self.main_window.set_minimize_to_tray_on_close(self.minimize_to_tray_on_close.isChecked())

            autostart_error = None
            if AutoStartManager.is_supported():
                try:
                    AutoStartManager.set_enabled(self.autostart.isChecked())
                except Exception as exc:
                    autostart_error = str(exc)

            self._refresh_autostart_status()
            if autostart_error:
                QMessageBox.warning(
                    self,
                    "Partially Saved",
                    f"Advanced settings saved, but failed to update auto-start: {autostart_error}",
                )
            else:
                QMessageBox.information(self, "Saved", "Advanced settings saved")
        except Exception as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._force_quit = False
        self._is_shutting_down = False
        self.tray_icon: QSystemTrayIcon | None = None

        gui_settings = _load_gui_settings()
        self.minimize_to_tray_on_close = bool(gui_settings.get("minimize_to_tray_on_close", False))

        self.setWindowTitle("nanobot GUI")
        self.resize(1320, 860)
        self.setWindowIcon(self._resolve_app_icon())

        self.store = ConfigStore()

        root = QWidget()
        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(6)

        splitter = QSplitter()
        splitter.setOrientation(Qt.Horizontal)

        self.nav = QListWidget()
        self.nav.setMinimumWidth(240)
        self.stack = QStackedWidget()

        splitter.addWidget(self.nav)
        splitter.addWidget(self.stack)
        splitter.setSizes([220, 1100])
        main_layout.addWidget(splitter)

        self.setCentralWidget(root)

        self.gateway_panel = GatewayPanel(self.store)
        self.providers_panel = ProvidersPanel(self.store)

        self._add_page("Gateway", self.gateway_panel)
        self._add_page("Skills", SkillsPanel(self.store))
        self._add_page("Providers", self.providers_panel)
        self._add_page("Channels", ChannelsPanel(self.store))
        self._add_page("MCP", MCPPanel(self.store))
        self._add_page("Cron", CronPanel())
        self._add_page("Others", AdvancedPanel(self.store, self))

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        self.setup_tray()

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

        logs = run_auto_onboard()
        self.store.reload()

        data_dir = get_data_dir()
        self.top_info = self.statusBar()
        self.top_info.showMessage(
            "Auto onboard completed | "
            f"config: {get_config_path()} | data: {data_dir} | "
            f"workspace: {get_workspace_path(self.store.config.agents.defaults.workspace)} | "
            + " | ".join(logs),
            0,
        )

    def _add_page(self, title: str, widget: QWidget) -> None:
        self.nav.addItem(title)
        self.stack.addWidget(widget)

    def _resolve_app_icon(self) -> QIcon:
        for icon_path in (
            Path("logo.png"),
            Path("icon.png"),
            Path(__file__).resolve().parents[2] / "logo.png",
            Path(__file__).resolve().parents[2] / "icon.png",
        ):
            icon = QIcon(str(icon_path))
            if not icon.isNull():
                return icon
        return self.style().standardIcon(QStyle.SP_ComputerIcon)

    def setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon = None
            return

        self.tray_icon = QSystemTrayIcon(self)
        tray_icon = self._resolve_app_icon()
        self.setWindowIcon(tray_icon)
        self.tray_icon.setIcon(tray_icon)
        self.tray_icon.setToolTip("nanobot GUI")

        tray_menu = QMenu(self)
        action_show = QAction("Show Window", self)
        action_quit = QAction("Quit", self)
        action_show.triggered.connect(self.show_from_tray)
        action_quit.triggered.connect(self.quit_from_tray)
        tray_menu.addAction(action_show)
        tray_menu.addSeparator()
        tray_menu.addAction(action_quit)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_from_tray()

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def set_minimize_to_tray_on_close(self, enabled: bool) -> None:
        self.minimize_to_tray_on_close = enabled
        settings = _load_gui_settings()
        settings["minimize_to_tray_on_close"] = enabled
        _save_gui_settings(settings)

    def quit_from_tray(self) -> None:
        self._force_quit = True
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def shutdown(self) -> None:
        if self._is_shutting_down:
            return
        self._is_shutting_down = True

        try:
            self.gateway_panel.shutdown()
        except Exception:
            pass

        try:
            self.providers_panel.shutdown()
        except Exception:
            pass

        if self.tray_icon is not None:
            self.tray_icon.hide()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange and self.isMinimized():
            if self.tray_icon is not None and self.tray_icon.isVisible():
                QTimer.singleShot(0, self.hide)

    def closeEvent(self, event: QCloseEvent) -> None:
        if (
            not self._force_quit
            and self.minimize_to_tray_on_close
            and self.tray_icon is not None
            and self.tray_icon.isVisible()
        ):
            self.hide()
            event.ignore()
            return

        self.shutdown()
        super().closeEvent(event)


if __name__ == "__main__":
    if hasattr(sys, "_MEIPASS"):
        os.chdir(sys._MEIPASS)
    else:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))

    app = QApplication(sys.argv)
    app_icon = QIcon(str(Path("logo.png")))
    if app_icon.isNull():
        app_icon = QIcon(str(Path("icon.png")))
    if app_icon.isNull():
        app_icon = QIcon(str(Path(__file__).resolve().parents[2] / "logo.png"))
    if app_icon.isNull():
        app_icon = QIcon(str(Path(__file__).resolve().parents[2] / "icon.png"))
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    app.setApplicationName("nanobot GUI")
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

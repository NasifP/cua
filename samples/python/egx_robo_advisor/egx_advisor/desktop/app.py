"""The desktop app: dashboard, chat and Thndr X in one window.

    desktop.cmd                     (Windows: double-click)
    .venv/bin/python desktop.py     (macOS / Linux)

One window with two tabs -- the dashboard (plan, log, chat, START/CONFIRM) and
Thndr X in the app's own Chromium browser -- and a halt button that is always
visible, with Ctrl+Shift+H as its shortcut.

What it keeps from the process design (docs/ARCHITECTURE.md, ADR-001):

- The agent is a separate process. The window, and so the halt button, stays
  responsive when the agent is wedged, and the halt goes straight to the bus.
- The bot starts halted and is armed only by the dashboard's two presses.
- Closing the window halts the bus before any process is stopped.

What the browser changes: the agent never sees the desktop. It reaches the
Thndr X tab only through `bridge.BridgeServer`, which offers three reads and
no input at all, so it cannot click anything anywhere -- on Thndr X, on the
dashboard, or on any other window.

Sign in to Thndr X yourself in its tab; the session is kept in state/browser
so it survives a restart. That folder holds a logged-in broker session: do not
copy or share it.
"""

from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import Future
from typing import Any, Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..doctor import FAIL, Check, check_api_keys, check_token, load_env, render
from ..launcher import Launcher, ProcessSpec, _halt_bus, _probe_port
from ..login_link import make_login_path
from ..paths import PROJECT_ROOT, bus_path
from ..safety.modes import ExecutionMode
from .bridge import BridgeServer

DEFAULT_THNDR_URL = "https://x.thndr.app"
BROWSER_PROFILE_DIR = PROJECT_ROOT / "state" / "browser"
READ_TIMEOUT = 15.0


# --------------------------------------------------------------------------- #
# The bridge's view of the Thndr X tab
# --------------------------------------------------------------------------- #


class QtPage(QObject):
    """BrowserPage over a QWebEngineView, callable from the bridge's threads.

    Qt objects belong to the GUI thread, so every read is handed to it through
    a queued signal and the calling thread waits on a Future.
    """

    _request = Signal(str, object)

    def __init__(self, view: QWebEngineView) -> None:
        super().__init__()
        self._view = view
        self._request.connect(self._handle, Qt.QueuedConnection)

    def _call(self, kind: str) -> Any:
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError("QtPage reads must come from a bridge thread")
        future: Future = Future()
        self._request.emit(kind, future)
        return future.result(timeout=READ_TIMEOUT)

    def info(self) -> dict[str, Any]:
        return self._call("info")

    def text(self) -> str:
        return self._call("text")

    def screenshot(self) -> bytes:
        return self._call("screenshot")

    @Slot(str, object)
    def _handle(self, kind: str, future: Future) -> None:
        try:
            if kind == "info":
                future.set_result({
                    "url": self._view.url().toString(),
                    "title": self._view.title(),
                    "loading": bool(getattr(self._view, "egx_loading", False)),
                })
            elif kind == "text":
                # An isolated world: the page's own scripts cannot see or
                # tamper with this read.
                self._view.page().runJavaScript(
                    "document.body ? document.body.innerText : ''",
                    QWebEngineScript.ApplicationWorld,
                    lambda result: future.set_result(result or ""),
                )
            elif kind == "screenshot":
                data = QByteArray()
                buffer = QBuffer(data)
                buffer.open(QIODevice.WriteOnly)
                self._view.grab().save(buffer, "PNG")
                future.set_result(bytes(data))
            else:
                future.set_exception(ValueError(kind))
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #


def _chrome_user_agent(profile: QWebEngineProfile) -> str:
    """Qt's user agent minus its QtWebEngine token: it is Chromium underneath."""
    parts = [p for p in profile.httpUserAgent().split() if not p.startswith("QtWebEngine/")]
    return " ".join(parts)


class MainWindow(QMainWindow):
    def __init__(self, env: dict[str, str], dashboard_port: int) -> None:
        super().__init__()
        self.env = env
        self.dashboard_port = dashboard_port
        self.setWindowTitle("EGX Robo-Advisor")
        self.resize(1400, 900)

        # --- the always-visible bar with the halt button ---
        self.status = QLabel("starting ...")
        self.status.setStyleSheet("font-size: 15px; padding: 0 12px;")
        self.halt_button = QPushButton("إيقاف البوت  |  HALT")
        self.halt_button.setStyleSheet(
            "QPushButton { background:#c62828; color:white; font-weight:bold; "
            "font-size:15px; padding:8px 22px; border-radius:6px; }"
        )
        self.halt_button.setToolTip("Stops the bot at once (Ctrl+Shift+H)")
        self.halt_button.clicked.connect(self.halt)
        QShortcut(QKeySequence("Ctrl+Shift+H"), self, activated=self.halt)

        bar = QHBoxLayout()
        bar.addWidget(self.status, 1)
        bar.addWidget(self.halt_button)

        # --- tabs ---
        self.dashboard = QWebEngineView()
        self.dashboard.setHtml("<p style='font-family:sans-serif'>Starting the dashboard ...</p>")

        self.profile = QWebEngineProfile("egx-thndr", self)
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self.profile.setPersistentStoragePath(str(BROWSER_PROFILE_DIR))
        self.profile.setCachePath(str(BROWSER_PROFILE_DIR / "cache"))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        self.profile.setHttpUserAgent(_chrome_user_agent(self.profile))
        self.thndr = QWebEngineView()
        self.thndr.setPage(QWebEnginePage(self.profile, self.thndr))
        self.thndr.egx_loading = True
        self.thndr.loadStarted.connect(lambda: setattr(self.thndr, "egx_loading", True))
        self.thndr.loadFinished.connect(lambda ok: setattr(self.thndr, "egx_loading", False))
        self.thndr.load(QUrl(env.get("EGX_THNDR_URL") or DEFAULT_THNDR_URL))

        self.tabs = QTabWidget()
        self.tabs.addTab(self.dashboard, "لوحة التحكم  |  Dashboard")
        self.tabs.addTab(self.thndr, "Thndr X")

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addLayout(bar)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(body)

        self.page = QtPage(self.thndr)
        self.bridge: Optional[BridgeServer] = None
        self.launcher: Optional[Launcher] = None
        self._dashboard_loaded = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    # ----------------------------------------------------------------- lifecycle

    def start_processes(self) -> None:
        self.bridge = BridgeServer(self.page).start()
        child_env = dict(os.environ)
        child_env.update(self.bridge.env())
        # No window to bring forward here: the agent reads the app's own browser.
        child_env["EGX_ARM_DELAY"] = "0"
        python = sys.executable
        specs = [
            ProcessSpec(
                "dashboard", [python, str(PROJECT_ROOT / "dashboard" / "app.py")],
                port=self.dashboard_port, health="/healthz",
            ),
            ProcessSpec(
                "agent",
                [python, str(PROJECT_ROOT / "run_agent.py"), "--target", "browser",
                 "--dry-run"],
            ),
        ]
        self.launcher = Launcher(specs=specs, env=child_env, say=self._say)
        self.launcher.start_all()

    def _say(self, message: str) -> None:
        print(message, flush=True)

    @Slot()
    def halt(self) -> None:
        try:
            _halt_bus("halt button in the desktop app", actor="desktop app")
            self.status.setText("HALTED by you")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Halt failed", f"Could not halt the bot: {exc}")

    def _tick(self) -> None:
        if self.launcher is not None:
            self.launcher.supervise_once()
        if not self._dashboard_loaded and _probe_port(self.dashboard_port, "/healthz") == "ours":
            self._dashboard_loaded = True
            link = make_login_path(self.env["EGX_DASHBOARD_TOKEN"])
            self.dashboard.load(QUrl(f"http://127.0.0.1:{self.dashboard_port}{link}"))
        self._refresh_status()

    def _refresh_status(self) -> None:
        try:
            from ..bus import StateBus

            with StateBus(bus_path()) as bus:
                control = bus.control_state()
                status = (bus.get("status") or {}).get("payload") or {}
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"bus unavailable: {exc}")
            return
        state = "HALTED" if control.halted else "ARMED"
        phase = status.get("phase") or "-"
        self.status.setText(f"{state}   ·   {phase}   ·   {control.reason or ''}")
        color = "#c62828" if control.halted else "#2e7d32"
        self.status.setStyleSheet(f"font-size:15px; padding:0 12px; color:{color};")

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        self._timer.stop()
        if self.launcher is not None:
            self.launcher.stop_all()  # halts the bus first
        else:
            _halt_bus("desktop app closed")
        if self.bridge is not None:
            self.bridge.stop()
        # A page must go before its profile, or Qt warns and may lose the
        # session it was about to write.
        page = self.thndr.page()
        self.thndr.setPage(QWebEnginePage(self.thndr))
        page.deleteLater()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #


def preflight(env: dict[str, str]) -> list[Check]:
    checks = [check_token(env)]
    mode_raw = env.get("EGX_MODE") or "live_read_only"
    try:
        mode = ExecutionMode.parse(mode_raw)
    except ValueError:
        mode = None
    if mode is not ExecutionMode.LIVE_READ_ONLY:
        checks.append(Check(
            "Mode", FAIL,
            f"EGX_MODE={mode_raw}: the desktop app runs live_read_only for now",
            "set EGX_MODE=live_read_only in .env",
        ))
    checks += [c for c in check_api_keys({**env, "EGX_MODE": "live_read_only"})
               if "screen agent" not in c.detail]
    return checks


def main(argv: Optional[list[str]] = None) -> int:
    env = load_env()
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})
    app = QApplication(argv if argv is not None else sys.argv)

    problems = [c for c in preflight(env) if c.status == FAIL]
    if problems:
        QMessageBox.critical(
            None, "EGX Robo-Advisor",
            "Fix these first (see .env), then start again:\n\n" + render(problems),
        )
        return 1

    window = MainWindow(env, int(env.get("EGX_DASHBOARD_PORT", "8787")))
    window.show()
    window.start_processes()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

"""The desktop app: dashboard, chat and Thndr X in one window.

    desktop.cmd                     (Windows: double-click)
    .venv/bin/python desktop.py     (macOS / Linux)

One window with a sidebar -- the dashboard (plan, log, chat, START/CONFIRM),
Thndr X in the app's own Chromium browser, the chart, the Strategy Lab, events
and news, and settings -- and a halt button at the bottom of the sidebar that
is always visible, with Ctrl+Shift+H as its shortcut. Colours and icons live
in theme.py.

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

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QObject,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import settings, spend
from ..doctor import FAIL, Check, check_api_keys, check_token, load_env, render
from ..launcher import Launcher, ProcessSpec, _halt_bus, _probe_port
from ..login_link import make_login_path
from ..paths import PROJECT_ROOT, bus_path
from ..safety.modes import ExecutionMode
from . import theme
from .bridge import BridgeServer
from .chart_tab import ChartTab
from .lab_tab import LabTab
from .settings_tab import SettingsTab
from .sources_tab import SourcesTab

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


def _placeholder(text: str) -> str:
    t = theme.tokens()
    return (
        f"<html><body style='margin:0;height:100vh;display:flex;align-items:center;"
        f"justify-content:center;background:{t['bg']};color:{t['muted']};"
        f"font:15px system-ui,sans-serif'>{text}</body></html>"
    )


def _chrome_user_agent(profile: QWebEngineProfile) -> str:
    """Qt's user agent minus its QtWebEngine token: it is Chromium underneath."""
    parts = [p for p in profile.httpUserAgent().split() if not p.startswith("QtWebEngine/")]
    return " ".join(parts)


class MainWindow(QMainWindow):
    def __init__(self, env: dict[str, str], dashboard_port: int,
                 notice: str = "") -> None:
        super().__init__()
        self.env = env
        self.dashboard_port = dashboard_port
        self.setWindowTitle("EGX Robo-Advisor")
        self.resize(1400, 900)

        # --- pages ---
        self.dashboard = QWebEngineView()
        self._theme_script: Optional[QWebEngineScript] = None
        self._sync_dashboard_theme()
        self.dashboard.setHtml(_placeholder("Starting the dashboard ..."))

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

        self.settings_tab = SettingsTab(on_saved=self.restart_processes)
        self.chart_tab = ChartTab()
        self.lab_tab = LabTab()
        self.sources_tab = SourcesTab()

        pages = (
            (self.dashboard, "dashboard", "لوحة التحكم", "Dashboard",
             "الخطة والمحفظة والسجل والشات. من هنا بتشغّل البوت."),
            (self.thndr, "browser", "Thndr X", "Your broker",
             "حسابك في متصفح البرنامج. البوت يقدر يقرا الصفحة دي بس، ومش بيضغط على حاجة."),
            (self.chart_tab, "chart", "الشارت", "Chart",
             "شارت TradingView لأسهمك. اللي هنا ليك انت، ومش بيوصل للبوت."),
            (self.lab_tab, "lab", "معمل الاستراتيجيات", "Strategy Lab",
             "جرّب قاعدة مؤشر على تاريخ البورصة الحقيقي قبل ما البوت يستخدمها."),
            (self.sources_tab, "calendar", "الأحداث والأخبار", "Events & news",
             "مواعيد ومصادر أخبار بتوقّف الشراء الجديد وقت الخطر."),
            (self.settings_tab, "settings", "الإعدادات", "Settings",
             "مفاتيح API والموديلات والحدود."),
        )
        self.pages = QStackedWidget()
        self._page_info: dict[QWidget, tuple[str, str]] = {}
        self._nav: list[tuple[QPushButton, str]] = []
        nav_group = QButtonGroup(self)
        nav_group.setExclusive(True)
        nav = QVBoxLayout()
        nav.setSpacing(4)
        for widget, icon_name, title, english, subtitle in pages:
            self.pages.addWidget(widget)
            self._page_info[widget] = (title, f"{english}  ·  {subtitle}")
            button = QPushButton(f"  {title}")
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setToolTip(english)
            button.setIconSize(QSize(20, 20))
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _=False, w=widget: self.show_page(w))
            nav_group.addButton(button)
            nav.addWidget(button)
            self._nav.append((button, icon_name))

        # --- sidebar: brand, navigation, theme, and the halt button ---
        mark = QLabel("EGX")
        mark.setObjectName("BrandMark")
        mark.setAlignment(Qt.AlignCenter)
        mark.setFixedSize(40, 40)
        brand_name = QLabel("Robo-Advisor")
        brand_name.setObjectName("BrandName")
        brand_sub = QLabel("مستشار البورصة المصرية")
        brand_sub.setObjectName("BrandSub")
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_text.addWidget(brand_name)
        brand_text.addWidget(brand_sub)
        brand = QHBoxLayout()
        brand.setSpacing(10)
        brand.addWidget(mark)
        brand.addLayout(brand_text, 1)

        self.theme_button = QPushButton()
        self.theme_button.setProperty("variant", "ghost")
        self.theme_button.setCursor(Qt.PointingHandCursor)
        self.theme_button.clicked.connect(self.toggle_theme)

        self.halt_button = QPushButton("  إيقاف البوت  ·  HALT")
        self.halt_button.setProperty("variant", "danger")
        self.halt_button.setIconSize(QSize(18, 18))
        self.halt_button.setCursor(Qt.PointingHandCursor)
        self.halt_button.setToolTip("Stops the bot at once (Ctrl+Shift+H)")
        self.halt_button.clicked.connect(self.halt)
        QShortcut(QKeySequence("Ctrl+Shift+H"), self, activated=self.halt)
        hint = QLabel("Ctrl+Shift+H  ·  الإيقاف ضغطة واحدة")
        hint.setObjectName("Hint")
        hint.setAlignment(Qt.AlignCenter)

        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(248)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 18, 14, 16)
        side.setSpacing(6)
        side.addLayout(brand)
        side.addSpacing(18)
        side.addLayout(nav)
        side.addStretch(1)
        side.addWidget(self.theme_button)
        side.addSpacing(6)
        side.addWidget(self.halt_button)
        side.addWidget(hint)

        # --- page header: title, and the bot's state at a glance ---
        self.page_title = QLabel()
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel()
        self.page_subtitle.setObjectName("PageSubtitle")
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_subtitle)

        self.state_pill = QLabel("...")
        self.state_pill.setProperty("pill", "muted")
        self.phase_pill = QLabel("-")
        self.phase_pill.setProperty("pill", "muted")
        self.spend_text = QLabel("الموديلات النهارده")
        self.spend_text.setObjectName("SpendText")
        # Its own label: "$0.00 / $2.00" inside Arabic text gets reordered.
        self.spend_value = QLabel("")
        self.spend_value.setObjectName("SpendValue")
        self.spend_value.setLayoutDirection(Qt.LeftToRight)
        spend_line = QHBoxLayout()
        spend_line.setSpacing(6)
        spend_line.addWidget(self.spend_text)
        spend_line.addStretch(1)
        spend_line.addWidget(self.spend_value)
        self.spend_bar = QProgressBar()
        self.spend_bar.setObjectName("Spend")
        self.spend_bar.setTextVisible(False)
        self.spend_bar.setRange(0, 1000)
        self.spend_bar.setFixedWidth(170)
        spend = QVBoxLayout()
        spend.setSpacing(4)
        spend.addLayout(spend_line)
        spend.addWidget(self.spend_bar)
        # Kept for anything that reads the old one-line status.
        self.status = self.state_pill

        header = QWidget()
        header.setObjectName("Header")
        head = QHBoxLayout(header)
        head.setContentsMargins(24, 16, 24, 14)
        head.setSpacing(12)
        head.addLayout(titles, 1)
        head.addWidget(self.state_pill)
        head.addWidget(self.phase_pill)
        head.addSpacing(8)
        head.addLayout(spend)

        main = QWidget()
        main.setObjectName("Page")
        column = QVBoxLayout(main)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(header)
        column.addWidget(self.pages, 1)

        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(sidebar)
        layout.addWidget(main, 1)
        self.setCentralWidget(body)
        self._paint_icons()

        self.show_page(self.dashboard)
        if notice:
            # Something needs setting before the bot can read anything: open there.
            self.show_page(self.settings_tab)
            self.settings_tab._say(notice, error=True)

        self.page = QtPage(self.thndr)
        self.bridge: Optional[BridgeServer] = None
        self.launcher: Optional[Launcher] = None
        self._dashboard_loaded = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    # ------------------------------------------------------------------ layout

    def show_page(self, widget: QWidget) -> None:
        self.pages.setCurrentWidget(widget)
        title, subtitle = self._page_info[widget]
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)
        for button, _ in self._nav:
            button.setChecked(False)
        self._nav[self.pages.indexOf(widget)][0].setChecked(True)

    def _paint_icons(self) -> None:
        for button, name in self._nav:
            button.setIcon(theme.icon(name))
        self.halt_button.setIcon(theme.icon("power", "#ffffff", "#ffffff"))
        dark = theme.current() == "dark"
        self.theme_button.setIcon(theme.icon("sun" if dark else "moon"))
        self.theme_button.setText("  وضع فاتح  ·  Light" if dark else "  وضع داكن  ·  Dark")

    @Slot()
    def toggle_theme(self) -> None:
        name = "light" if theme.current() == "dark" else "dark"
        theme.apply(QApplication.instance(), name)
        self._paint_icons()
        self._sync_dashboard_theme(run_now=True)
        self.chart_tab.set_theme(name)
        self._refresh_status()
        try:
            env_file = settings.ENV_FILE
            text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
            settings.ENV_FILE.write_text(settings.set_env_values(text, {"EGX_THEME": name}),
                                         encoding="utf-8")
        except OSError:
            pass  # the theme still changed; it just will not be remembered

    def _sync_dashboard_theme(self, run_now: bool = False) -> None:
        """The dashboard page follows the window's theme, on every load."""
        # At DocumentCreation there is no <html> element yet: set it as soon as
        # one exists, so the page never flashes the other theme.
        source = (
            "(function(){var t=%r;function s(){var e=document.documentElement;"
            "if(!e)return false;e.dataset.theme=t;return true}"
            "if(!s()){new MutationObserver(function(_,o){if(s())o.disconnect()})"
            ".observe(document,{childList:true})}})();" % theme.current()
        )
        scripts = self.dashboard.page().scripts()
        if self._theme_script is not None:
            scripts.remove(self._theme_script)
        script = QWebEngineScript()
        script.setName("egx-theme")
        script.setInjectionPoint(QWebEngineScript.DocumentCreation)
        script.setWorldId(QWebEngineScript.MainWorld)
        script.setSourceCode(source)
        scripts.insert(script)
        self._theme_script = script
        if run_now:
            self.dashboard.page().runJavaScript(source, QWebEngineScript.ApplicationWorld)

    # ----------------------------------------------------------------- lifecycle

    def start_processes(self, bridge_running: bool = False) -> None:
        if not bridge_running:
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

    def restart_processes(self) -> None:
        """Pick up saved settings: halt, stop the dashboard and agent, start again.

        The bridge and the Thndr X session stay as they are.
        """
        if self.launcher is not None:
            self.launcher.stop_all()  # halts the bus first
            self.launcher = None
        os.environ.update(settings.effective_values())
        self._dashboard_loaded = False
        self.dashboard.setHtml(_placeholder("Restarting ..."))
        self.start_processes(bridge_running=True)

    @Slot()
    def halt(self) -> None:
        try:
            _halt_bus("halt button in the desktop app", actor="desktop app")
            self._set_pill(self.state_pill, "متوقف  ·  HALTED", "bad")
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
                totals = spend.today(bus)
        except Exception as exc:  # noqa: BLE001
            self._set_pill(self.state_pill, "الحالة غير معروفة  ·  UNKNOWN", "warn")
            self.state_pill.setToolTip(f"bus unavailable: {exc}")
            return
        if control.halted:
            self._set_pill(self.state_pill, "متوقف  ·  HALTED", "bad")
        else:
            self._set_pill(self.state_pill, "شغّال  ·  ARMED", "ok")
        self.state_pill.setToolTip(control.reason or "")
        self._set_pill(self.phase_pill, status.get("phase") or "idle", "muted")

        call_limit, usd_limit = spend.limits()
        usd, calls = float(totals.get("usd", 0)), int(totals.get("calls", 0))
        used = max(usd / usd_limit if usd_limit else 1.0, calls / call_limit if call_limit else 1.0)
        self.spend_value.setText(f"${usd:.2f} / ${usd_limit:.2f}")
        self.spend_bar.setToolTip(spend.describe(totals))
        self.spend_text.setToolTip(spend.describe(totals))
        self.spend_bar.setValue(int(min(used, 1.0) * 1000))
        level = "bad" if used >= 1 else "warn" if used >= 0.8 else ""
        if self.spend_bar.property("level") != level:
            self.spend_bar.setProperty("level", level)
            theme.restyle(self.spend_bar)

    @staticmethod
    def _set_pill(label: QLabel, text: str, kind: str) -> None:
        label.setText(text)
        if label.property("pill") != kind:
            label.setProperty("pill", kind)
            theme.restyle(label)

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
        self.chart_tab.release()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #


def ensure_dashboard_password(env: dict[str, str]) -> None:
    """Generate the dashboard password into .env if it is missing or short.

    The app signs in to its own dashboard with a one-time link, so the operator
    never needs to see or type this.
    """
    if check_token(env).status != FAIL:
        return
    import secrets as _secrets

    token = _secrets.token_urlsafe(32)
    text = settings.ENV_FILE.read_text(encoding="utf-8") if settings.ENV_FILE.exists() else ""
    settings.ENV_FILE.write_text(
        settings.set_env_values(text, {"EGX_DASHBOARD_TOKEN": token}), encoding="utf-8"
    )
    env["EGX_DASHBOARD_TOKEN"] = token
    os.environ["EGX_DASHBOARD_TOKEN"] = token


def preflight(env: dict[str, str]) -> list[Check]:
    """Problems that stop the app. Missing API keys are not among them: the app
    opens on Settings instead, where they can be entered."""
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
    return checks


def missing_keys(env: dict[str, str]) -> list[Check]:
    return [
        c for c in check_api_keys({**env, "EGX_MODE": "live_read_only"})
        if c.status == FAIL and "screen agent" not in c.detail
    ]


def main(argv: Optional[list[str]] = None) -> int:
    env = load_env()
    os.environ.update({k: v for k, v in env.items() if v or k not in os.environ})
    app = QApplication(argv if argv is not None else sys.argv)
    theme.apply(app, theme.name_from_env(env))
    ensure_dashboard_password(env)

    problems = [c for c in preflight(env) if c.status == FAIL]
    if problems:
        QMessageBox.critical(
            None, "EGX Robo-Advisor",
            "Fix these first (see .env), then start again:\n\n" + render(problems),
        )
        return 1

    missing = missing_keys(env)
    notice = ""
    if missing:
        notice = (
            "Before the bot can read your portfolio: "
            + "; ".join(f"{c.name} is {c.detail}" for c in missing)
            + ". Enter the key below and press Save."
        )
    window = MainWindow(env, int(env.get("EGX_DASHBOARD_PORT", "8787")), notice=notice)
    window.show()
    window.start_processes()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

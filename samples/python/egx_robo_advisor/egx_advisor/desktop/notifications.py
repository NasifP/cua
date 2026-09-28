"""Desktop alerts: stop hit, first target reached, news brake / risk-off.

NotificationManager watches the bus on a background thread (a plain Python
thread: SQLite reads and nothing else) and hands new alerts to the UI thread
through a Qt signal; the tray icon shows them as native Windows notifications
(Action Center toasts), including while the window is minimized or hidden.
Qt's own QSystemTrayIcon does this without an extra package.

Which alerts, and "only once a day", are decided in alerts.py; the ledger is
on the bus, so a restart does not repeat an alert.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QSystemTrayIcon

from .. import alerts
from ..i18n import current as current_lang

logger = logging.getLogger(__name__)

#: How often the bus is read. The agent refreshes prices once a cycle.
INTERVAL_SECONDS = 30.0


class NotificationManager(QObject):
    """Polls for alerts on its own thread; shows them from the UI thread."""

    #: title, body, kind -- emitted from the worker thread, delivered queued.
    alert = Signal(str, str, str)

    def __init__(self, open_bus: Callable[[], Any], icon: QIcon,
                 on_clicked: Callable[[], None] = lambda: None,
                 interval: float = INTERVAL_SECONDS,
                 tray: Optional[QSystemTrayIcon] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.open_bus = open_bus
        self.interval = interval
        self.tray = tray or QSystemTrayIcon(icon, self)
        self.tray.setToolTip("EGX Robo-Advisor")
        self.tray.messageClicked.connect(on_clicked)
        self.tray.activated.connect(lambda _reason: on_clicked())
        self.alert.connect(self._show)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: What was shown, newest last (for tests and a future history view).
        self.shown: list[tuple[str, str, str]] = []

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> NotificationManager:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="egx-alerts", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.tray.hide()

    # ------------------------------------------------------------------ worker thread

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:  # noqa: BLE001 - an alert is extra; keep watching
                logger.exception("alert check failed")
            self._stop.wait(self.interval)

    def poll(self) -> list[alerts.Alert]:
        """One check: claim new alerts and emit them. Runs on the worker thread."""
        from ..clock import CAIRO

        with self.open_bus() as bus:
            found = alerts.check(bus.all_snapshots())
            fresh = alerts.Ledger(bus).claim(found, datetime.now(CAIRO).date())
        lang = current_lang()
        for a in fresh:
            title, body = alerts.message(a, lang)
            # Queued to the UI thread: this object lives there.
            self.alert.emit(title, body, a.kind)
        return fresh

    # ------------------------------------------------------------------ UI thread

    @Slot(str, str, str)
    def _show(self, title: str, body: str, kind: str) -> None:
        self.shown.append((title, body, kind))
        icon = QSystemTrayIcon.Critical if kind in (alerts.STOP, alerts.BRAKE) \
            else QSystemTrayIcon.Information
        if self.tray.isVisible():
            self.tray.showMessage(title, body, icon, 15_000)

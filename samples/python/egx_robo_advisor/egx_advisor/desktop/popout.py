"""Thndr X in its own window, for a second screen.

The Thndr X page (browser and ticket panel) can leave the main window and
live in a window of its own: the market on one screen, the app's controls on
the other. The ticket panel moves with it. The browser view itself is never
moved: on Windows a live web view moved into another window stops drawing and
takes no clicks, so the app closes it and opens a new one in the new window,
at the same address and on the same profile. The Thndr X sign-in is kept (the
profile's cookies are persistent); the page loads again.

Choosing Thndr X in the navigation opens the separate window; "Back to main
window" keeps the page in the main window from then on, and "Open in a
separate window" returns to the first. The choice is remembered
(state/desktop.ini), and so is where the window was, so it opens on the same
screen next time. The first time, with two screens, it opens maximised on the
screen the main window is not on. Closing the separate window brings the page
back until Thndr X is chosen again. The window carries its own HALT button: with the main window on
the other screen, stopping the bot must not need a trip across.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr
from . import theme

SEPARATE_KEY = "thndr/separate"
GEOMETRY_KEY = "thndr/geometry"


class DetachedWindow(QWidget):
    """The separate window. Closing it docks the page back."""

    def __init__(self, on_close: Callable[[], None], on_dock: Callable[[], None],
                 on_halt: Callable[[], None]) -> None:
        super().__init__(None, Qt.Window)
        self.setObjectName("Page")
        self._on_close = on_close
        self.closing_for_good = False

        self.dock_button = QPushButton()
        self.dock_button.setProperty("variant", "ghost")
        self.dock_button.setCursor(Qt.PointingHandCursor)
        self.dock_button.clicked.connect(on_dock)
        self.halt_button = QPushButton()
        self.halt_button.setProperty("variant", "danger")
        self.halt_button.setIconSize(QSize(16, 16))
        self.halt_button.setCursor(Qt.PointingHandCursor)
        self.halt_button.clicked.connect(on_halt)

        bar = QHBoxLayout()
        bar.setContentsMargins(10, 6, 10, 6)
        bar.addWidget(self.dock_button)
        bar.addStretch(1)
        bar.addWidget(self.halt_button)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        self.body.addLayout(bar)
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(f"{tr('page.thndr')} - EGX Robo-Advisor")
        self.dock_button.setText("  " + tr("popout.dock"))
        self.dock_button.setIcon(theme.icon("dashboard"))
        self.halt_button.setText("  " + tr("app.halt"))
        self.halt_button.setToolTip(tr("app.halt_tip"))
        self.halt_button.setIcon(theme.icon("power", "#ffffff", "#ffffff"))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self.closing_for_good:
            self._on_close()
        super().closeEvent(event)


class PopOut(QWidget):
    """The Thndr X slot in the main window's pages.

    Holds `content` while docked, with a button to move it out; shows a note
    with "show window" and "bring back" while it is out.
    """

    def __init__(self, content: QWidget, on_halt: Callable[[], None],
                 settings_file: Path,
                 before_move: Optional[Callable[[], None]] = None,
                 after_move: Optional[Callable[[], None]] = None) -> None:
        super().__init__()
        self.content = content
        #: Called around every move. The app closes the browser view before
        #: and opens a new one after, so no live web view is ever moved
        #: between windows (see MainWindow._close_thndr_view).
        self._before_move = before_move or (lambda: None)
        self._after_move = after_move or (lambda: None)
        self.settings = QSettings(str(settings_file), QSettings.IniFormat)
        self.window_: Optional[DetachedWindow] = None
        self._on_halt = on_halt

        # Docked: a slim bar above the page.
        self.undock_button = QPushButton()
        self.undock_button.setProperty("variant", "ghost")
        self.undock_button.setCursor(Qt.PointingHandCursor)
        self.undock_button.clicked.connect(lambda: self.detach(remember=True))
        self.docked_bar = QWidget()
        bar = QHBoxLayout(self.docked_bar)
        bar.setContentsMargins(10, 6, 10, 6)
        bar.addStretch(1)
        bar.addWidget(self.undock_button)

        # Detached: a note in place of the page.
        self.note = QLabel()
        self.note.setObjectName("Hint")
        self.note.setAlignment(Qt.AlignCenter)
        self.note.setWordWrap(True)
        self.show_button = QPushButton()
        self.show_button.setProperty("variant", "primary")
        self.show_button.setCursor(Qt.PointingHandCursor)
        self.show_button.clicked.connect(self.raise_window)
        self.back_button = QPushButton()
        self.back_button.setProperty("variant", "ghost")
        self.back_button.setCursor(Qt.PointingHandCursor)
        self.back_button.clicked.connect(lambda: self.dock(remember=True))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.show_button)
        buttons.addWidget(self.back_button)
        buttons.addStretch(1)
        self.detached_note = QWidget()
        note = QVBoxLayout(self.detached_note)
        note.addStretch(1)
        note.addWidget(self.note)
        note.addSpacing(12)
        note.addLayout(buttons)
        note.addStretch(1)
        self.detached_note.hide()

        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(0)
        self.layout_.addWidget(self.docked_bar)
        self.layout_.addWidget(self.content, 1)
        self.layout_.addWidget(self.detached_note, 1)
        self.retranslate()

    # ------------------------------------------------------------------ state

    @property
    def detached(self) -> bool:
        return self.window_ is not None and self.window_.isVisible()

    @property
    def prefers_separate(self) -> bool:
        return str(self.settings.value(SEPARATE_KEY, "true")).lower() == "true"

    def _remember_choice(self, separate: bool) -> None:
        self.settings.setValue(SEPARATE_KEY, "true" if separate else "false")
        self.settings.sync()

    def opened(self) -> None:
        """The Thndr X page was chosen in the navigation."""
        if self.prefers_separate:
            self.detach(remember=False)

    # ------------------------------------------------------------------ moving

    def detach(self, remember: bool = False) -> None:
        if remember:
            self._remember_choice(True)
        if self.detached:
            self.raise_window()
            return
        if self.window_ is None:
            self.window_ = DetachedWindow(on_close=self._window_closed,
                                          on_dock=lambda: self.dock(remember=True),
                                          on_halt=self._on_halt)
        self._before_move()
        self.layout_.removeWidget(self.content)
        self.window_.body.addWidget(self.content, 1)
        self.content.show()
        self.docked_bar.hide()
        self.detached_note.show()
        self._place(self.window_)
        self._after_move()
        self.raise_window()

    def dock(self, remember: bool = False) -> None:
        if remember:
            self._remember_choice(False)
        window = self.window_
        if window is None or self.content.parent() is self:
            return  # already here
        self._before_move()
        if window is not None:
            self.settings.setValue(GEOMETRY_KEY, window.saveGeometry())
            self.settings.sync()
            window.body.removeWidget(self.content)
            window.closing_for_good = True
            window.close()
            window.closing_for_good = False
        self.layout_.insertWidget(1, self.content, 1)
        self.content.show()
        self.detached_note.hide()
        self.docked_bar.show()
        self._after_move()

    def _window_closed(self) -> None:
        # Closed with the window's X: the page comes back for now, and the
        # next click on Thndr X opens the window again. "Back to main window"
        # is what keeps it here.
        self.dock(remember=False)

    def raise_window(self) -> None:
        if self.window_ is None:
            return
        if self.window_.isMinimized():
            self.window_.showNormal()
        self.window_.raise_()
        self.window_.activateWindow()

    def _place(self, window: DetachedWindow) -> None:
        saved = self.settings.value(GEOMETRY_KEY)
        if saved is not None and window.restoreGeometry(saved):
            window.show()
            return
        # First time: with a second screen, fill the screen the app is not on.
        here = self.window().screen() if self.window() is not self else None
        others = [s for s in QApplication.screens() if s is not here]
        if here is not None and others:
            window.setGeometry(others[0].availableGeometry())
            window.showMaximized()
            return
        window.resize(1280, 820)
        window.show()

    def shutdown(self) -> None:
        """The app is closing: close the window without docking back."""
        if self.window_ is not None:
            if self.window_.isVisible():
                self.settings.setValue(GEOMETRY_KEY, self.window_.saveGeometry())
                self.settings.sync()
            self.window_.closing_for_good = True
            self.window_.close()

    def retranslate(self) -> None:
        self.undock_button.setText("  " + tr("popout.undock"))
        self.undock_button.setIcon(theme.icon("browser"))
        self.note.setText(tr("popout.note"))
        self.show_button.setText(tr("popout.show"))
        self.back_button.setText(tr("popout.dock"))
        if self.window_ is not None:
            self.window_.retranslate()

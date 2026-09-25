"""The Events & sources tab: the economic calendar the bot respects, and its news feeds.

Events: dates when new buys stop (rate decisions, inflation prints). Copy them
from cbe.org.eg, CAPMAS, or an economic calendar such as Investing.com's; the
bot does not fetch them (see regime/events.py).

Feeds: RSS/Atom sources for the news brake. "Official" marks an exchange or
regulator feed: if it fails during a session, buys stop. Check each site's
terms before adding it. Both apply from the bot's next cycle.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr
from ..paths import config_path
from ..regime.events import IMPACTS, ScheduledEvent, load_events, save_events
from ..regime.sources import read_feed_entries, save_feeds
from . import theme

EVENTS_FILE = config_path("events.toml")
FEEDS_FILE = config_path("feeds.toml")


def _table(columns: int) -> QTableWidget:
    table = QTableWidget(0, columns)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setHighlightSections(False)
    return table


class SourcesTab(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.message = QLabel("")
        self.message.setWordWrap(True)

        # --- events ---
        self.events = _table(3)
        self.event_date = QDateEdit(QDate.currentDate())
        self.event_date.setCalendarPopup(True)
        self.event_date.setDisplayFormat("yyyy-MM-dd")
        self.event_title = QLineEdit()
        self.event_impact = QComboBox()
        self.event_impact.addItems(list(IMPACTS))
        self.add_event_button = QPushButton()
        self.add_event_button.setProperty("variant", "primary")
        self.add_event_button.clicked.connect(self.add_event)
        self.remove_event_button = QPushButton()
        self.remove_event_button.setProperty("variant", "ghost")
        self.remove_event_button.clicked.connect(self.remove_event)
        event_row = QHBoxLayout()
        for widget in (self.event_date, self.event_title, self.event_impact,
                       self.add_event_button, self.remove_event_button):
            event_row.addWidget(widget, 1 if widget is self.event_title else 0)
        self.events_box = QGroupBox()
        events_layout = QVBoxLayout(self.events_box)
        self.events_note = QLabel()
        self.events_note.setProperty("muted", "true")
        self.events_note.setWordWrap(True)
        events_layout.addWidget(self.events_note)
        self.events_empty = QLabel()
        self.events_empty.setProperty("message", "info")
        events_layout.addWidget(self.events_empty)
        events_layout.addWidget(self.events, 1)
        events_layout.addLayout(event_row)

        # --- feeds ---
        self.feeds = _table(4)
        self.feed_name = QLineEdit()
        self.feed_url = QLineEdit()
        self.feed_official = QCheckBox()
        self.add_feed_button = QPushButton()
        self.add_feed_button.setProperty("variant", "primary")
        self.add_feed_button.clicked.connect(self.add_feed)
        self.toggle_feed_button = QPushButton()
        self.toggle_feed_button.setProperty("variant", "ghost")
        self.toggle_feed_button.clicked.connect(self.toggle_feed)
        self.remove_feed_button = QPushButton()
        self.remove_feed_button.setProperty("variant", "ghost")
        self.remove_feed_button.clicked.connect(self.remove_feed)
        feed_row = QHBoxLayout()
        feed_row.addWidget(self.feed_name)
        feed_row.addWidget(self.feed_url, 2)
        feed_row.addWidget(self.feed_official)
        feed_row.addWidget(self.add_feed_button)
        feed_buttons = QHBoxLayout()
        feed_buttons.addStretch(1)
        feed_buttons.addWidget(self.toggle_feed_button)
        feed_buttons.addWidget(self.remove_feed_button)
        self.feeds_box = QGroupBox()
        feeds_layout = QVBoxLayout(self.feeds_box)
        self.feeds_note = QLabel()
        self.feeds_note.setProperty("muted", "true")
        self.feeds_note.setWordWrap(True)
        feeds_layout.addWidget(self.feeds_note)
        feeds_layout.addWidget(self.feeds, 1)
        feeds_layout.addLayout(feed_row)
        feeds_layout.addLayout(feed_buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 8, 24, 20)
        layout.setSpacing(4)
        layout.addWidget(self.events_box, 1)
        layout.addWidget(self.feeds_box, 1)
        layout.addWidget(self.message)
        self.retranslate()

    def retranslate(self) -> None:
        self.events.setHorizontalHeaderLabels(
            [tr("src.col_date"), tr("src.col_event"), tr("src.col_impact")])
        self.event_title.setPlaceholderText(tr("src.event_placeholder"))
        self.add_event_button.setText(tr("src.add_event"))
        self.remove_event_button.setText(tr("src.remove"))
        self.events_box.setTitle(tr("src.calendar"))
        self.events_note.setText(tr("src.calendar_note"))
        self.events_empty.setText(tr("src.no_events"))
        self.feeds.setHorizontalHeaderLabels([tr("src.col_name"), tr("src.col_url"),
                                              tr("src.col_official"), tr("src.col_status")])
        self.feed_name.setPlaceholderText(tr("src.name"))
        self.feed_url.setPlaceholderText(tr("src.url"))
        self.feed_official.setText(tr("src.official"))
        self.add_feed_button.setText(tr("src.add_feed"))
        self.toggle_feed_button.setText(tr("src.toggle"))
        self.remove_feed_button.setText(tr("src.remove"))
        self.feeds_box.setTitle(tr("src.news"))
        self.feeds_note.setText(tr("src.news_note"))
        # URLs read left to right whatever the language.
        self.feed_url.setLayoutDirection(Qt.LeftToRight)
        self._say("")
        self.reload()

    # ------------------------------------------------------------------ events

    def reload(self) -> None:
        try:
            self._events = list(load_events(EVENTS_FILE))
        except Exception as exc:  # noqa: BLE001
            self._events = []
            self._say(tr("src.events_unreadable", error=exc), error=True)
        self.events.setRowCount(0)
        self.events_empty.setVisible(not self._events)
        for event in self._events:
            row = self.events.rowCount()
            self.events.insertRow(row)
            for col, text in enumerate((event.day.isoformat(), event.title, event.impact)):
                self.events.setItem(row, col, QTableWidgetItem(text))
        try:
            self._feeds = read_feed_entries(FEEDS_FILE)
        except Exception as exc:  # noqa: BLE001
            self._feeds = []
            self._say(tr("src.feeds_unreadable", error=exc), error=True)
        self.feeds.setRowCount(0)
        for entry in self._feeds:
            row = self.feeds.rowCount()
            self.feeds.insertRow(row)
            cells = (entry["name"], entry["url"],
                     tr("src.official_yes") if entry["authoritative"] else "",
                     tr("src.on") if entry["enabled"] else tr("src.off"))
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if not entry["enabled"]:
                    item.setForeground(Qt.gray)
                self.feeds.setItem(row, col, item)

    def add_event(self) -> None:
        title = self.event_title.text().strip()
        if not title:
            self._say(tr("src.need_title"), error=True)
            return
        day = self.event_date.date().toPython()
        self._events.append(ScheduledEvent(day, title, self.event_impact.currentText()))
        save_events(EVENTS_FILE, self._events)
        self.event_title.clear()
        self._say(tr("src.added_event", title=title, day=day))
        self.reload()

    def remove_event(self) -> None:
        row = self.events.currentRow()
        if 0 <= row < len(self._events):
            removed = self._events.pop(row)
            save_events(EVENTS_FILE, self._events)
            self._say(tr("src.removed_event", title=removed.title))
            self.reload()

    # ------------------------------------------------------------------- feeds

    def add_feed(self) -> None:
        url = self.feed_url.text().strip()
        if not url.startswith(("https://", "http://")):
            self._say(tr("src.need_url"), error=True)
            return
        self._feeds.append({
            "name": self.feed_name.text().strip() or url,
            "url": url,
            "authoritative": self.feed_official.isChecked(),
            "enabled": True,
        })
        save_feeds(FEEDS_FILE, self._feeds)
        self.feed_name.clear()
        self.feed_url.clear()
        self.feed_official.setChecked(False)
        self._say(tr("src.added_feed"))
        self.reload()

    def toggle_feed(self) -> None:
        row = self.feeds.currentRow()
        if 0 <= row < len(self._feeds):
            self._feeds[row]["enabled"] = not self._feeds[row]["enabled"]
            save_feeds(FEEDS_FILE, self._feeds)
            self._say(tr("src.saved"))
            self.reload()

    def remove_feed(self) -> None:
        row = self.feeds.currentRow()
        if 0 <= row < len(self._feeds):
            self._feeds.pop(row)
            save_feeds(FEEDS_FILE, self._feeds)
            self._say(tr("src.removed_feed"))
            self.reload()

    def _say(self, text: str, *, error: bool = False) -> None:
        theme.say(self.message, text, "bad" if error else "ok")

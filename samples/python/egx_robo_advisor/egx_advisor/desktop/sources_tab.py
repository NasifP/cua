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

from ..paths import config_path
from ..regime.events import IMPACTS, ScheduledEvent, load_events, save_events
from ..regime.sources import read_feed_entries, save_feeds
from . import theme

EVENTS_FILE = config_path("events.toml")
FEEDS_FILE = config_path("feeds.toml")


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
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
        self.events = _table(["التاريخ", "الحدث", "الأهمية"])
        self.event_date = QDateEdit(QDate.currentDate())
        self.event_date.setCalendarPopup(True)
        self.event_date.setDisplayFormat("yyyy-MM-dd")
        self.event_title = QLineEdit()
        self.event_title.setPlaceholderText("مثلاً: قرار الفايدة من البنك المركزي")
        self.event_impact = QComboBox()
        self.event_impact.addItems(list(IMPACTS))
        add_event = QPushButton("أضف الحدث")
        add_event.setProperty("variant", "primary")
        add_event.clicked.connect(self.add_event)
        remove_event = QPushButton("احذف المحدد")
        remove_event.setProperty("variant", "ghost")
        remove_event.clicked.connect(self.remove_event)
        event_row = QHBoxLayout()
        for widget in (self.event_date, self.event_title, self.event_impact, add_event,
                       remove_event):
            event_row.addWidget(widget, 1 if widget is self.event_title else 0)
        events_box = QGroupBox("التقويم الاقتصادي")
        events_layout = QVBoxLayout(events_box)
        note = QLabel(
            "مفيش شراء جديد حوالين الأحداث دي. high: اليوم اللي قبل الحدث ويومه واليوم اللي "
            "بعده. medium: يوم الحدث بس. انقل المواعيد من موقع البنك المركزي أو "
            "Investing.com."
        )
        note.setProperty("muted", "true")
        note.setToolTip("No new buys around these: high = the day before, of and after; "
                        "medium = the day of.")
        note.setWordWrap(True)
        events_layout.addWidget(note)
        self.events_empty = QLabel(
            "لسه مفيش أحداث. ضيف ميعاد اجتماع البنك المركزي الجاي من cbe.org.eg.")
        self.events_empty.setProperty("message", "info")
        events_layout.addWidget(self.events_empty)
        events_layout.addWidget(self.events, 1)
        events_layout.addLayout(event_row)

        # --- feeds ---
        self.feeds = _table(["الاسم", "الرابط", "رسمي", "الحالة"])
        self.feed_name = QLineEdit()
        self.feed_name.setPlaceholderText("الاسم")
        self.feed_url = QLineEdit()
        self.feed_url.setPlaceholderText("https://...  رابط RSS أو Atom")
        self.feed_official = QCheckBox("مصدر رسمي (البورصة أو الرقابة)")
        self.feed_official.setToolTip("If an official source fails during a session, buys stop")
        add_feed = QPushButton("أضف المصدر")
        add_feed.setProperty("variant", "primary")
        add_feed.clicked.connect(self.add_feed)
        toggle_feed = QPushButton("شغّل / وقّف")
        toggle_feed.setProperty("variant", "ghost")
        toggle_feed.clicked.connect(self.toggle_feed)
        remove_feed = QPushButton("احذف المحدد")
        remove_feed.setProperty("variant", "ghost")
        remove_feed.clicked.connect(self.remove_feed)
        feed_row = QHBoxLayout()
        feed_row.addWidget(self.feed_name)
        feed_row.addWidget(self.feed_url, 2)
        feed_row.addWidget(self.feed_official)
        feed_row.addWidget(add_feed)
        feed_buttons = QHBoxLayout()
        feed_buttons.addStretch(1)
        feed_buttons.addWidget(toggle_feed)
        feed_buttons.addWidget(remove_feed)
        feeds_box = QGroupBox("مصادر الأخبار")
        feeds_layout = QVBoxLayout(feeds_box)
        feed_note = QLabel(
            "روابط RSS بس، ومن غير تسجيل دخول. اتأكد إن شروط الموقع بتسمح قبل ما تضيفه. "
            "لو مصدر رسمي وقع وقت التداول، الشراء بيقف. التعديل بيشتغل بعد إعادة تشغيل البوت."
        )
        feed_note.setProperty("muted", "true")
        feed_note.setWordWrap(True)
        feeds_layout.addWidget(feed_note)
        feeds_layout.addWidget(self.feeds, 1)
        feeds_layout.addLayout(feed_row)
        feeds_layout.addLayout(feed_buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 8, 24, 20)
        layout.setSpacing(4)
        layout.addWidget(events_box, 1)
        layout.addWidget(feeds_box, 1)
        layout.addWidget(self.message)
        self.reload()

    # ------------------------------------------------------------------ events

    def reload(self) -> None:
        try:
            self._events = list(load_events(EVENTS_FILE))
        except Exception as exc:  # noqa: BLE001
            self._events = []
            self._say(f"events.toml unreadable: {exc}", error=True)
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
            self._say(f"feeds.toml unreadable: {exc}", error=True)
        self.feeds.setRowCount(0)
        for entry in self._feeds:
            row = self.feeds.rowCount()
            self.feeds.insertRow(row)
            cells = (entry["name"], entry["url"], "رسمي" if entry["authoritative"] else "",
                     "شغّال" if entry["enabled"] else "متوقف")
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if not entry["enabled"]:
                    item.setForeground(Qt.gray)
                self.feeds.setItem(row, col, item)

    def add_event(self) -> None:
        title = self.event_title.text().strip()
        if not title:
            self._say("Give the event a title.", error=True)
            return
        day = self.event_date.date().toPython()
        self._events.append(ScheduledEvent(day, title, self.event_impact.currentText()))
        save_events(EVENTS_FILE, self._events)
        self.event_title.clear()
        self._say(f"Added {title} on {day}. Applies from the bot's next cycle.")
        self.reload()

    def remove_event(self) -> None:
        row = self.events.currentRow()
        if 0 <= row < len(self._events):
            removed = self._events.pop(row)
            save_events(EVENTS_FILE, self._events)
            self._say(f"Removed {removed.title}.")
            self.reload()

    # ------------------------------------------------------------------- feeds

    def add_feed(self) -> None:
        url = self.feed_url.text().strip()
        if not url.startswith(("https://", "http://")):
            self._say("A feed needs an http(s) URL.", error=True)
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
        self._say("Feed added. The bot polls it after its next restart.")
        self.reload()

    def toggle_feed(self) -> None:
        row = self.feeds.currentRow()
        if 0 <= row < len(self._feeds):
            self._feeds[row]["enabled"] = not self._feeds[row]["enabled"]
            save_feeds(FEEDS_FILE, self._feeds)
            self._say("Saved. Applies after the bot's next restart.")
            self.reload()

    def remove_feed(self) -> None:
        row = self.feeds.currentRow()
        if 0 <= row < len(self._feeds):
            self._feeds.pop(row)
            save_feeds(FEEDS_FILE, self._feeds)
            self._say("Removed. Applies after the bot's next restart.")
            self.reload()

    def _say(self, text: str, *, error: bool = False) -> None:
        theme.say(self.message, text, "bad" if error else "ok")

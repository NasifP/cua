"""The Memory tab: see, correct and delete everything the app remembers.

Four parts, each over memory.py:

- My notes: preferences, why you hold a stock (why, risks, when to exit) and
  plain notes. Add, edit and delete them here, or say "remember that ..." in
  the chat. The chat is given these as your words.
- Conversations: what the chat remembers of earlier questions. Clear it here.
- Lab journal: every Strategy Lab run and its verdict.
- Pick review: how the scan's picks and the bot's plan orders did 5, 20 and 60
  sessions later. "Update prices" fetches the prices the review needs.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import date
from typing import Any, Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..i18n import tr
from ..memory import KINDS, Memory
from ..review import HORIZONS, describe, review, scorecard
from . import theme
from .sources_tab import _table


class _Signals(QObject):
    prices = Signal(str)


class MemoryTab(QWidget):
    def __init__(self, memory: Memory, archive: Optional[Any] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.memory = memory
        self.archive = archive
        self._notes: list = []
        self._editing: Optional[int] = None
        self._signals = _Signals()
        self._signals.prices.connect(self._prices_done)
        self.message = QLabel("")
        self.message.setWordWrap(True)

        # --- my notes ---
        self.notes_table = _table(3)
        header = self.notes_table.horizontalHeader()
        for col in (0, 1):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.notes_table.itemSelectionChanged.connect(self._load_selected)
        self.kind = QComboBox()
        self.symbol = QLineEdit()
        self.symbol.setMaximumWidth(120)
        self.symbol.setLayoutDirection(Qt.LeftToRight)
        self.text = QPlainTextEdit()
        self.text.setMaximumHeight(110)
        self.save_button = QPushButton()
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self.save_note)
        self.new_button = QPushButton()
        self.new_button.setProperty("variant", "ghost")
        self.new_button.clicked.connect(self.new_note)
        self.delete_button = QPushButton()
        self.delete_button.setProperty("variant", "ghost")
        self.delete_button.clicked.connect(self.delete_note)
        self.notes_hint = QLabel()
        self.notes_hint.setProperty("muted", "true")
        self.notes_hint.setWordWrap(True)
        form_top = QHBoxLayout()
        form_top.addWidget(self.kind)
        form_top.addWidget(self.symbol)
        form_top.addStretch(1)
        form_top.addWidget(self.new_button)
        form_top.addWidget(self.delete_button)
        form_top.addWidget(self.save_button)
        notes = QWidget()
        notes_layout = QVBoxLayout(notes)
        notes_layout.addWidget(self.notes_hint)
        notes_layout.addWidget(self.notes_table, 1)
        notes_layout.addLayout(form_top)
        notes_layout.addWidget(self.text)

        # --- conversations ---
        self.talk = QPlainTextEdit()
        self.talk.setReadOnly(True)
        self.clear_talk_button = QPushButton()
        self.clear_talk_button.setProperty("variant", "ghost")
        self.clear_talk_button.clicked.connect(self.clear_conversation)
        talk = QWidget()
        talk_layout = QVBoxLayout(talk)
        talk_layout.addWidget(self.talk, 1)
        talk_row = QHBoxLayout()
        talk_row.addStretch(1)
        talk_row.addWidget(self.clear_talk_button)
        talk_layout.addLayout(talk_row)

        # --- lab journal ---
        self.lab_table = _table(4)
        lab = QWidget()
        QVBoxLayout(lab).addWidget(self.lab_table)

        # --- pick review ---
        self.review_summary = QLabel()
        self.review_summary.setWordWrap(True)
        self.review_table = _table(4 + len(HORIZONS))
        self.update_button = QPushButton()
        self.update_button.setProperty("variant", "primary")
        self.update_button.clicked.connect(self.update_prices)
        self.review_hint = QLabel()
        self.review_hint.setProperty("muted", "true")
        self.review_hint.setWordWrap(True)
        picks = QWidget()
        picks_layout = QVBoxLayout(picks)
        picks_layout.addWidget(self.review_hint)
        picks_layout.addWidget(self.review_summary)
        picks_layout.addWidget(self.review_table, 1)
        picks_row = QHBoxLayout()
        picks_row.addStretch(1)
        picks_row.addWidget(self.update_button)
        picks_layout.addLayout(picks_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(notes, "")
        self.tabs.addTab(talk, "")
        self.tabs.addTab(lab, "")
        self.tabs.addTab(picks, "")
        self.tabs.currentChanged.connect(lambda _: self.reload())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 8, 24, 20)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.message)
        self.retranslate()

    # ------------------------------------------------------------------ text

    def retranslate(self) -> None:
        for index, key in enumerate(("mem.notes", "mem.talk", "mem.lab", "mem.picks")):
            self.tabs.setTabText(index, tr(key))
        current = self.kind.currentData()
        self.kind.clear()
        for kind in KINDS:
            self.kind.addItem(tr(f"mem.kind.{kind}"), kind)
        if current:
            self.kind.setCurrentIndex(max(0, self.kind.findData(current)))
        self.symbol.setPlaceholderText(tr("mem.symbol"))
        self.text.setPlaceholderText(tr("mem.text_placeholder"))
        self.save_button.setText(tr("mem.save"))
        self.new_button.setText(tr("mem.new"))
        self.delete_button.setText(tr("mem.delete"))
        self.notes_hint.setText(tr("mem.notes_hint"))
        self.notes_table.setHorizontalHeaderLabels(
            [tr("mem.col_kind"), tr("mem.col_symbol"), tr("mem.col_text")])
        self.clear_talk_button.setText(tr("mem.clear_talk"))
        self.lab_table.setHorizontalHeaderLabels(
            [tr("mem.col_date"), tr("mem.col_test"), tr("mem.col_verdict"),
             tr("mem.col_evidence")])
        self.review_table.setHorizontalHeaderLabels(
            [tr("mem.col_date"), tr("mem.col_source"), tr("mem.col_symbol"),
             tr("mem.col_price")] + [tr("mem.col_after", n=h) for h in HORIZONS])
        self.review_hint.setText(tr("mem.review_hint"))
        self.update_button.setText(tr("mem.update_prices"))
        self.reload()

    def _say(self, text: str, *, error: bool = False) -> None:
        theme.say(self.message, text, "bad" if error else "ok")

    # ------------------------------------------------------------------ views

    def reload(self) -> None:
        try:
            index = self.tabs.currentIndex()
            if index == 0:
                self._reload_notes()
            elif index == 1:
                self.talk.setPlainText("\n\n".join(
                    f"{e.ts[:16].replace('T', ' ')}\n> {e.question}\n{e.answer}"
                    for e in self.memory.recent_exchanges(50)) or tr("mem.no_talk"))
            elif index == 2:
                self._fill(self.lab_table, [
                    (r.ts[:10], r.title + (" *" if r.synthetic else ""), r.verdict, r.evidence)
                    for r in self.memory.lab_runs()])
            else:
                self._reload_review()
        except Exception as exc:  # noqa: BLE001
            self._say(tr("mem.unreadable", error=exc), error=True)

    @staticmethod
    def _fill(table: Any, rows: list[tuple]) -> None:
        table.setRowCount(0)
        for values in rows:
            row = table.rowCount()
            table.insertRow(row)
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(str(value)))

    def _reload_notes(self) -> None:
        self._notes = self.memory.notes()
        self.notes_table.blockSignals(True)
        self._fill(self.notes_table, [
            (tr(f"mem.kind.{n.kind}"), n.symbol.removesuffix(".CA"), n.text.replace("\n", " "))
            for n in self._notes])
        self.notes_table.blockSignals(False)

    def _closes(self, symbol: str) -> list:
        if self.archive is None:
            return []
        return [(r.day, float(r.close)) for r in self.archive.series(symbol)]

    def _reload_review(self) -> None:
        results = review(self.memory.picks(), self._closes)
        arabic = i18n.current() == "ar"
        self.review_summary.setText(describe(scorecard(results), arabic))
        sources = {"scan": tr("mem.source.scan"), "plan": tr("mem.source.plan")}
        self._fill(self.review_table, [
            (r.pick.day.isoformat(), sources.get(r.pick.source, r.pick.source) +
             (f" ({tr('mem.sell')})" if r.pick.side == "sell" else ""),
             r.pick.symbol.removesuffix(".CA"), f"{r.pick.price:.2f}",
             *("-" if r.moves[h] is None else f"{r.moves[h]:+.1f}%" for h in HORIZONS))
            for r in results])

    # ------------------------------------------------------------------ notes

    @Slot()
    def _load_selected(self) -> None:
        rows = self.notes_table.selectionModel().selectedRows()
        if not rows or not 0 <= rows[0].row() < len(self._notes):
            return
        note = self._notes[rows[0].row()]
        self._editing = note.id
        self.kind.setCurrentIndex(max(0, self.kind.findData(note.kind)))
        self.symbol.setText(note.symbol.removesuffix(".CA"))
        self.text.setPlainText(note.text)

    @Slot()
    def new_note(self) -> None:
        self._editing = None
        self.notes_table.clearSelection()
        self.symbol.clear()
        self.text.clear()
        if self.kind.currentData() == "thesis":
            self.text.setPlainText(tr("mem.thesis_template"))
        self.text.setFocus()

    @Slot()
    def save_note(self) -> None:
        kind, symbol, text = self.kind.currentData(), self.symbol.text(), self.text.toPlainText()
        if kind == "thesis" and not symbol.strip():
            self._say(tr("mem.need_symbol"), error=True)
            return
        try:
            if self._editing is None:
                self._editing = self.memory.add_note(kind, text, symbol=symbol)
            else:
                self.memory.update_note(self._editing, kind, text, symbol=symbol)
        except ValueError as exc:
            self._say(str(exc), error=True)
            return
        self._say(tr("mem.saved"))
        self._reload_notes()

    @Slot()
    def delete_note(self) -> None:
        """Delete the note in the form: the one last selected or saved."""
        rows = self.notes_table.selectionModel().selectedRows()
        if rows and 0 <= rows[0].row() < len(self._notes):
            self._editing = self._notes[rows[0].row()].id
        if self._editing is None:
            return
        self.memory.delete_note(self._editing)
        self.new_note()
        self._say(tr("mem.deleted"))
        self._reload_notes()

    @Slot()
    def clear_conversation(self) -> None:
        self.memory.clear_conversation()
        self._say(tr("mem.cleared"))
        self.reload()

    # ------------------------------------------------------------------ prices

    @Slot()
    def update_prices(self) -> None:
        """Fetch prices since the oldest pick, into the archive, off the GUI thread."""
        picks = self.memory.picks()
        if not picks or self.archive is None:
            self._say(tr("mem.no_picks"))
            return
        self.update_button.setEnabled(False)
        self._say(tr("mem.fetching"))
        symbols = sorted({p.symbol for p in picks})
        days = (date.today() - min(p.day for p in picks)).days + 120

        def work() -> None:
            try:
                from ..marketdata import YahooMarketData
                from ..marketdata.archive import ArchivedMarketData
                from ..types import Instrument, Sleeve

                source = ArchivedMarketData(
                    YahooMarketData(include_macro=False, enforce_quality=False), self.archive)
                asyncio.run(source.history([Instrument(s, s, Sleeve.BLUE_CHIP) for s in symbols],
                                           days=max(days, 30)))
                self._signals.prices.emit(tr("mem.archived") if source.stale else "")
            except Exception as exc:  # noqa: BLE001 - network
                self._signals.prices.emit(f"{type(exc).__name__}: {exc}")

        threading.Thread(target=work, daemon=True).start()

    @Slot(str)
    def _prices_done(self, problem: str) -> None:
        self.update_button.setEnabled(True)
        if problem:
            self._say(problem, error=True)
        else:
            self._say(tr("mem.updated"))
        self._reload_review()


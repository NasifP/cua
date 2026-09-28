"""The Training tab: indicators saved, drawn and studied; ticket boxes taught here."""

from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("PySide6.QtWidgets")

from egx_advisor import indicators as ind  # noqa: E402
from egx_advisor.memory import Memory  # noqa: E402

from tests.test_indicators import rows, wave  # noqa: E402


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class FakeWebPage:
    def __init__(self):
        self.scripts = []

    def runJavaScript(self, script, world, callback):  # noqa: N802 - Qt API
        self.scripts.append(script)
        if "__egxPicked || null" in script:
            return callback(json.dumps({"ok": True, "candidates": ['input[name="qty"]'],
                                        "placeholder": "", "label": "Qty"}))
        return callback(json.dumps({"ok": True}))


@pytest.fixture
def tab(app, tmp_path, monkeypatch):
    from egx_advisor.desktop import teach_box, training_tab

    monkeypatch.setattr(teach_box, "TICKET_FILE", tmp_path / "thndr.ticket.toml")
    memory = Memory(tmp_path / "m.db")
    applied, taught, started = [], [], []
    history = {f"S{i}.CA": rows(wave(n=800, amp=8 + i)) for i in range(3)}
    page = FakeWebPage()
    widget = training_tab.TrainingTab(
        memory, page=lambda: page, history=lambda: history, on_apply=applied.append,
        on_taught=lambda: taught.append(1), on_teach_start=lambda: started.append(1))
    widget.applied, widget.taught, widget.started, widget.page = applied, taught, started, page
    return widget


def test_saving_keeps_the_set_and_draws_it_on_the_chart(tab):
    row = next(r for r in range(tab.table.rowCount())
               if tab.table.item(r, 0).data(0x0100) == "rsi")
    tab.table.item(row, 1).setText("9, 25, 75")
    assert tab.save()
    saved = ind.load_set(tab.memory.ui_get(Memory.INDICATORS_KEY))
    assert next(c for c in saved if c.key == "rsi").label() == "rsi(9, 25, 75)"
    assert tab.applied[-1] == ["MASimple@tv-basicstudies", "RSI@tv-basicstudies",
                               "MACD@tv-basicstudies"]


def test_arabic_digits_and_commas_are_read(tab):
    row = next(r for r in range(tab.table.rowCount())
               if tab.table.item(r, 0).data(0x0100) == "rsi")
    tab.table.item(row, 1).setText("⁦٩، ٢٥، ٧٥⁩")
    rsi = next(c for c in tab.read_table() if c.key == "rsi")
    assert rsi.label() == "rsi(9, 25, 75)"


def test_a_bad_setting_is_refused_and_nothing_is_saved(tab):
    tab.table.item(0, 1).setText("1")
    assert not tab.save()
    assert tab.memory.ui_get(Memory.INDICATORS_KEY) is None and tab.applied == []


def test_studying_keeps_results_in_memory_and_shows_them(tab):
    import time

    from PySide6.QtCore import QCoreApplication

    tab.study()
    for _ in range(200):
        QCoreApplication.processEvents()
        if not tab.studying:
            break
        time.sleep(0.02)
    assert not tab.studying
    studies = tab.memory.indicator_studies()
    assert {s["key"] for s in studies} == {"sma", "rsi", "macd"}
    assert tab.results.rowCount() == 3


def test_ticket_boxes_are_taught_from_this_tab(tab, tmp_path):
    tab.teach_box.teach("quantity")
    assert tab.started == [1] and tab.teach_box.picking == "quantity"
    tab.teach_box._poll_picker()
    assert tab.teach_box.picking is None and tab.taught == [1]
    assert "quantity" in (tmp_path / "thndr.ticket.toml").read_text(encoding="utf-8")


def test_the_ticket_panel_no_longer_teaches(app):
    from egx_advisor.desktop.ticket_panel import TicketPanel

    panel = TicketPanel(page=lambda: FakeWebPage())
    assert not hasattr(panel, "teach_box") and not hasattr(panel, "teach")

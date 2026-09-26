"""The Memory tab and the Lab journal in the desktop app."""

from __future__ import annotations

import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWebEngineWidgets")

from egx_advisor.memory import Memory, Pick  # noqa: E402


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def memory(tmp_path):
    return Memory(tmp_path / "memory.db")


def test_notes_are_written_edited_and_deleted_from_the_tab(app, memory):
    from egx_advisor.desktop.memory_tab import MemoryTab

    tab = MemoryTab(memory)
    tab.kind.setCurrentIndex(tab.kind.findData("thesis"))
    tab.new_note()
    assert tab.text.toPlainText(), "a thesis starts from the why / risks / exit template"
    tab.save_note()
    assert memory.notes() == [], "a thesis needs a ticker"

    tab.symbol.setText("comi")
    tab.text.setPlainText("Why: dividends\nExit if: payout cut")
    tab.save_button.click()
    [note] = memory.notes()
    assert (note.kind, note.symbol) == ("thesis", "COMI.CA")

    tab.notes_table.selectRow(0)
    tab.text.setPlainText("Why: dividends and growth")
    tab.save_button.click()
    assert memory.notes()[0].text == "Why: dividends and growth"
    assert len(memory.notes()) == 1, "saving a selected note edits it"

    tab.notes_table.selectRow(0)
    tab.delete_button.click()
    assert memory.notes() == []


def test_the_other_tabs_show_conversation_lab_runs_and_the_review(app, memory, tmp_path):
    from egx_advisor.desktop.memory_tab import MemoryTab
    from egx_advisor.marketdata import YahooRow
    from egx_advisor.marketdata.archive import PriceArchive

    memory.add_exchange("why halted?", "because you pressed HALT")
    memory.record_lab_run("k", "rsi 30 (5y)", 1200, False, False, "noise", "+0.1%/yr")
    day = date(2026, 1, 1)
    memory.record_picks([Pick(day, "scan", "COMI.CA", "buy", 100.0, "4/4")])
    archive = PriceArchive(tmp_path / "prices.db")
    archive.store({"COMI.CA": [YahooRow(day + timedelta(days=i), 100 + i, 100 + i, 100 + i,
                                        100 + i, 1000) for i in range(1, 8)]}, day, day)
    tab = MemoryTab(memory, archive)

    tab.tabs.setCurrentIndex(1)
    assert "because you pressed HALT" in tab.talk.toPlainText()
    tab.clear_talk_button.click()
    assert memory.recent_exchanges() == []

    tab.tabs.setCurrentIndex(2)
    assert tab.lab_table.item(0, 1).text() == "rsi 30 (5y)"

    tab.tabs.setCurrentIndex(3)
    assert tab.review_table.item(0, 2).text() == "COMI"
    assert tab.review_table.item(0, 4).text() == "+5.0%"
    assert tab.review_table.item(0, 5).text() == "-"


def test_a_repeated_lab_test_says_when_it_was_run_before(app, memory):
    from egx_advisor.desktop.lab_tab import LabTab

    report = SimpleNamespace(
        rules=(), title="report.four_factor", sessions=1200, passed=False,
        verdict=lambda lang="en": "not better than the plan", evidence=lambda: "+0.0%/yr",
        render=lambda lang="en": "REPORT")
    tab = LabTab(memory=memory)
    tab._run_span = (5, False)
    tab._journal(report)
    assert tab._previous is None
    tab._journal(report)
    assert tab._previous is not None
    assert len(memory.lab_runs()) == 2
    tab._report = report
    tab._show_result()
    assert "not better than the plan" in tab.output.toPlainText().split("REPORT", 1)[1]

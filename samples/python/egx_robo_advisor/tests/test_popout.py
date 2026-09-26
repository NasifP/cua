"""Thndr X in its own window: the page moves, is never rebuilt, and comes back."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _slot(tmp_path, halts=None):
    halts = [] if halts is None else halts
    from PySide6.QtWidgets import QLabel

    from egx_advisor.desktop.popout import PopOut

    content = QLabel("thndr")
    slot = PopOut(content, on_halt=lambda: halts.append(1),
                  settings_file=tmp_path / "desktop.ini")
    return slot, content


def test_choosing_thndr_opens_it_in_its_own_window_by_default(app, tmp_path):
    slot, content = _slot(tmp_path)
    slot.opened()
    assert slot.detached
    assert content.window() is slot.window_
    slot.shutdown()


def test_back_to_main_window_is_remembered(app, tmp_path):
    slot, content = _slot(tmp_path)
    slot.opened()
    slot.window_.dock_button.click()
    assert not slot.detached and content.parent() is slot
    slot.opened()
    assert not slot.detached, "the choice to keep it docked sticks"

    again, _ = _slot(tmp_path)
    assert not again.prefers_separate, "and survives a restart"
    slot.undock_button.click()
    assert slot.detached and slot.prefers_separate
    slot.shutdown()


def test_closing_the_window_brings_the_page_back_without_changing_the_choice(app, tmp_path):
    slot, content = _slot(tmp_path)
    slot.opened()
    slot.window_.close()
    assert not slot.detached and content.parent() is slot
    assert slot.prefers_separate
    slot.opened()
    assert slot.detached
    slot.shutdown()


def test_the_separate_window_has_its_own_halt_button(app, tmp_path):
    halts: list[int] = []
    slot, _ = _slot(tmp_path, halts)
    slot.opened()
    slot.window_.halt_button.click()
    assert halts == [1]
    slot.shutdown()


def test_shutdown_closes_the_window_and_keeps_the_choice(app, tmp_path):
    slot, _ = _slot(tmp_path)
    slot.opened()
    slot.shutdown()
    assert not slot.window_.isVisible()
    assert slot.prefers_separate


def test_the_app_is_told_before_and_after_every_move(app, tmp_path):
    """The app swaps the web view around a move instead of moving a live one."""
    from PySide6.QtWidgets import QLabel

    from egx_advisor.desktop.popout import PopOut

    calls: list[str] = []
    content = QLabel("thndr")
    slot = PopOut(content, on_halt=lambda: None, settings_file=tmp_path / "d.ini",
                  before_move=lambda: calls.append(f"before:{content.window() is slot.window()}"),
                  after_move=lambda: calls.append(f"after:{content.window() is slot.window()}"))
    slot.opened()
    assert calls == ["before:True", "after:False"], "closed in the old window, opened in the new"
    slot.window_.dock_button.click()
    assert calls[2:] == ["before:False", "after:True"]
    slot.dock()
    assert len(calls) == 4, "docking when already docked moves nothing"
    slot.shutdown()

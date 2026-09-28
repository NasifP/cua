"""The ticket panel with a buy the analyst prepared: listed first, filled once the ticket opens."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PySide6.QtWidgets")

from egx_advisor.bus import StateBus  # noqa: E402
from egx_advisor.execution import ticket_fill as tf  # noqa: E402


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class FakeWebPage:
    """Answers the panel's scripts: the ticket is closed until `open_ticket`."""

    def __init__(self):
        self.ticket_open = False
        self.values = {}
        self.fills = 0

    def runJavaScript(self, script, world, callback):  # noqa: N802 - Qt API
        if "setValue" in script:
            if not self.ticket_open:
                return callback(json.dumps({"ok": False, "reason": "field_missing",
                                            "field": "quantity"}))
            self.fills += 1
            self.values = {"quantity": "10", "price": "90.5"}
            return callback(json.dumps({"ok": True}))
        if "out[name] = el.value" in script:
            return callback(json.dumps({"ok": True, **self.values}))
        return callback("null")


@pytest.fixture
def panel(app, tmp_path, monkeypatch):
    from egx_advisor.desktop import ticket_panel as tp

    monkeypatch.setenv("EGX_BUS_PATH", str(tmp_path / "bus.db"))
    monkeypatch.setenv("EGX_TICKET_FILL", "true")
    ticket = tmp_path / "thndr.ticket.toml"
    tf.save_ticket_map(ticket, tf.TicketMap({
        "quantity": tf.FieldSpec(('input[name="qty"]',)),
        "price": tf.FieldSpec(('input[name="price"]',))}))
    monkeypatch.setattr(tp, "TICKET_FILE", ticket)
    page = FakeWebPage()
    widget = tp.TicketPanel(page=lambda: page)
    widget.fake = page
    # Read-backs run at once instead of after a delay.
    monkeypatch.setattr(tp.QTimer, "singleShot", staticmethod(lambda ms, fn: fn()))
    return widget


def _propose(tmp_path, minutes_ago=1, autofill=True):
    now = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    with StateBus(tmp_path / "bus.db") as bus:
        bus.resume(actor="test")
        bus.put("ticket_proposal", {
            "symbol": "COMI.CA", "side": "buy", "quantity": "10", "limit_price": "90.5",
            "rationale": "trend", "ts": now.isoformat(),
            "autofill_until": (now + timedelta(minutes=3 if autofill else -1)).isoformat()})


def test_the_prepared_buy_is_listed_first_and_filled_once_the_ticket_opens(panel, tmp_path):
    _propose(tmp_path)
    panel.refresh()
    assert panel.orders.item(0).text().startswith(("Analyst", "من المحلل"))
    assert panel.fake.fills == 0  # ticket not open yet: waiting, nothing written
    panel.refresh()
    panel.fake.ticket_open = True
    panel.refresh()
    assert panel.fake.fills == 1
    assert panel._message[0] == "ticket.filled"
    panel.refresh()
    panel.refresh()
    assert panel.fake.fills == 1, "an automatic fill happens once, never again"


def test_halt_stops_the_automatic_fill(panel, tmp_path):
    _propose(tmp_path)
    with StateBus(tmp_path / "bus.db") as bus:
        bus.halt(actor="test", reason="stop")
    panel.fake.ticket_open = True
    panel.refresh()
    assert panel.fake.fills == 0 and panel._message[0] == "ticket.halted"


def test_the_switch_stops_the_automatic_fill(panel, tmp_path, monkeypatch):
    monkeypatch.setenv("EGX_TICKET_FILL", "false")
    _propose(tmp_path)
    panel.fake.ticket_open = True
    panel.refresh()
    assert panel.fake.fills == 0


def test_after_the_window_only_fill_fills(panel, tmp_path):
    _propose(tmp_path, autofill=False)
    panel.fake.ticket_open = True
    panel.refresh()
    assert panel.fake.fills == 0
    panel.orders.setCurrentRow(0)
    panel.fill()
    assert panel.fake.fills == 1


def test_an_old_proposal_is_not_listed(panel, tmp_path):
    _propose(tmp_path, minutes_ago=20)
    panel.fake.ticket_open = True
    panel.refresh()
    assert panel._proposal is None and panel.fake.fills == 0

"""Sprint 4 widgets: cards, alerts, Omnibar and Quick View, chart Signals view (offscreen)."""

from __future__ import annotations

import os
import threading
import time
from datetime import date

import pytest

pytest.importorskip("PySide6.QtWebEngineWidgets")

from egx_advisor import daily
from egx_advisor.bus import StateBus


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Chromium refuses to start its sandbox as root (CI containers): the web
    # views here load only local test pages.
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def wait_for(app, done, timeout=5.0):
    """Spin the event loop until `done()` (results arrive as queued signals)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if done():
            return True
        time.sleep(0.01)
    return False


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, operation, **args):
        self.calls.append((operation, args, threading.current_thread().name))
        return {"ok": True, "url": "https://x.thndr.app/stocks/" + args.get("symbol", "")}


class FakeToolbox:
    def __init__(self, seen):
        self.seen = seen

    def prepare_buy(self, symbol, quantity, limit_price, reason=""):
        self.seen.append(("prepare_buy", symbol, quantity, limit_price,
                          threading.current_thread().name))
        return {"prepared": True, "order": {"symbol": symbol + ".CA"}}

    def search_news(self, query, limit=8):
        self.seen.append(("news", query))
        return {"items": [{"title": f"{query} profit up", "source": "X", "date": "2026-09-27",
                           "url": "https://example.com/a"}]}

    def liquidity(self, ticker):
        from egx_advisor.market_state import LiquidityCap

        return LiquidityCap(10_000, 3, 300)


class FakeServices:
    def __init__(self, tmp_path, slow=""):
        self.path = tmp_path / "s.db"
        StateBus(self.path).close()
        self.client = FakeClient()
        self.seen = []
        #: A ticker whose opinion takes a while (to test stale answers).
        self.slow = slow

    def bus(self):
        return StateBus(self.path)

    def toolbox(self, bus):
        return FakeToolbox(self.seen)

    def browse(self):
        return self.client

    def ask_team(self, question):
        if self.slow and self.slow in question:
            time.sleep(0.4)
        self.seen.append(("ask", question))
        ticker = next(t for t in ("COMI", "TMGH", "ETEL") if t in question)
        return f"opinion about {ticker}"


# --------------------------------------------------------------------------- Today


def test_a_buy_card_prepares_off_the_ui_thread_and_opens_thndr(app, tmp_path):
    from egx_advisor.desktop.today_tab import TodayTab

    services = FakeServices(tmp_path)
    opened = []
    tab = TodayTab(services, on_opened=lambda: opened.append(True))
    card = daily.Card("ETEL", "buy", "four factors agree", "plan", quantity=40,
                      limit_price=30.0, can_prepare=True)
    tab.show_cards([card], {"ETEL": 40})
    widget = tab.cards[0]
    assert widget.button.isEnabled() and widget.quantity.value() == 40
    widget.button.click()
    assert wait_for(app, lambda: opened)
    name = services.seen[0][4]
    assert services.seen[0][:4] == ("prepare_buy", "ETEL", 40, 30.0)
    assert name.startswith("egx-ui"), "prepare_buy must not run on the UI thread"


def test_the_teams_cap_is_checked_before_prepare(app, tmp_path):
    from egx_advisor.desktop.today_tab import TodayTab

    services = FakeServices(tmp_path)
    tab = TodayTab(services)
    card = daily.Card("SWDY", "buy", "team", "team", limit_price=50.0, max_egp=900,
                      can_prepare=True)
    tab.show_cards([card], {"SWDY": 100})     # 100 x 50 = 5,000 > 900
    tab.cards[0].button.click()
    assert wait_for(app, lambda: tab.cards[0].status.text() and "..." not in
                    tab.cards[0].status.text())
    assert services.seen == [] and "900" in tab.cards[0].status.text()


def test_a_sell_card_opens_the_stock_instead(app, tmp_path):
    from egx_advisor.desktop.today_tab import TodayTab

    services = FakeServices(tmp_path)
    tab = TodayTab(services)
    tab.show_cards([daily.Card("COMI", "sell", "under its stop", "levels")], {})
    assert tab.cards[0].quantity is None
    tab.cards[0].button.click()
    assert wait_for(app, lambda: services.client.calls)
    assert services.client.calls[0][:2] == ("open_stock", {"symbol": "COMI"})


def test_cards_load_from_the_bus(app, tmp_path):
    from egx_advisor.desktop.today_tab import TodayTab, load_cards

    services = FakeServices(tmp_path)
    with services.bus() as bus:
        bus.put("plan", {"orders": [{"symbol": "ETEL.CA", "side": "buy", "quantity": "40",
                                     "limit_price": "30", "rationale": "rules"}]})
        bus.put("portfolio", {"cash_egp": "600", "positions": []})
        bus.resume(actor="test")
    cards, quantities, cash = load_cards(services)
    assert [c.ticker for c in cards] == ["ETEL"] and quantities["ETEL"] == 20 and cash == 600
    tab = TodayTab(services)
    tab.refresh()
    assert wait_for(app, lambda: tab.cards)


# --------------------------------------------------------------------------- alerts


def test_notifications_poll_on_a_thread_and_show_on_the_ui_thread(app, tmp_path):
    from PySide6.QtGui import QIcon

    from egx_advisor.desktop.notifications import NotificationManager

    path = tmp_path / "s.db"
    with StateBus(path) as bus:
        bus.put("portfolio", {"positions": [{"symbol": "COMI.CA", "quantity": "10",
                                             "market_value": "750"}]})
        bus.put("levels", {"levels": {"COMI.CA": {"stop": 80, "target1": 95}}})
    manager = NotificationManager(open_bus=lambda: StateBus(path), icon=QIcon(),
                                  interval=0.05).start()
    try:
        assert wait_for(app, lambda: manager.shown)
    finally:
        manager.stop()
    assert len(manager.shown) == 1 and manager.shown[0][2] == "stop"
    assert manager.poll() == [], "the same alert is not sent twice in a day"


# --------------------------------------------------------------------------- Omnibar


def test_the_omnibar_accepts_tickers_only(app):
    from egx_advisor.desktop.omnibar import Omnibar

    bar = Omnibar()
    got = []
    bar.searched.connect(got.append)
    bar.set_tickers(["COMI.CA", "TMGH"])
    bar.setText(" comi.ca ")
    bar.returnPressed.emit()
    bar.setText("not a ticker!!")
    bar.returnPressed.emit()
    assert got == ["COMI"]


def test_quick_view_fills_all_panes_and_drops_stale_answers(app, tmp_path):
    from egx_advisor.desktop.omnibar import QuickView

    services = FakeServices(tmp_path, slow="COMI")
    view = QuickView(services, studies=lambda: None, go_thndr=lambda: None)
    view.show_ticker("COMI")
    view.show_ticker("TMGH")      # a second search before the first answered
    asked = lambda: sum(1 for s in services.seen if s[0] == "ask")
    # Both opinions arrive (the slow COMI one last), news and Thndr X too.
    assert wait_for(app, lambda: asked() == 2 and view.news.count() > 0
                    and ":" in view.thndr_status.text()
                    and "..." not in view.thndr_status.text(), timeout=5)
    wait_for(app, lambda: False, timeout=0.3)   # deliver COMI's late callback
    assert view.opinion.toPlainText() == "opinion about TMGH"
    assert "TMGH" in view.news.item(0).text()
    assert ("open_stock", {"symbol": "TMGH"}) in [c[:2] for c in services.client.calls]
    view.release()


# --------------------------------------------------------------------------- chart


def test_the_signals_view_loads_marks_off_the_ui_thread(app):
    from egx_advisor.desktop.chart_tab import ChartTab
    from tests.test_analyst import _bars

    threads = []

    def loader(symbol):
        threads.append(threading.current_thread().name)
        return _bars(150), [{"time": "2026-09-01", "position": "belowBar", "color": "#0f0",
                             "shape": "arrowUp", "text": "4F in"}], [], "legend"

    tab = ChartTab(marks_loader=loader)
    tab.set_mode("signals")
    tab.show_symbol("COMI.CA")
    assert wait_for(app, lambda: threads)
    assert threads[0].startswith("egx-ui") and tab.mode == "signals"
    tab.release()


def test_load_marks_uses_the_bus_view_and_levels(tmp_path):
    from egx_advisor.desktop.chart_tab import load_marks
    from tests.test_analyst import _bars

    path = tmp_path / "s.db"
    with StateBus(path) as bus:
        bus.put("team_views", {"COMI": {"decision": "buy", "day": date.today().isoformat()}})
    rows, marks, lines, legend = load_marks(
        "COMI", history=lambda symbols, days: {s: _bars(300) for s in symbols},
        open_bus=lambda: StateBus(path))
    assert rows and marks[-1]["text"] == "Team: buy" and {l["title"] for l in lines} >= {"Stop"}


# --------------------------------------------------------------------------- review fixes


def test_a_callback_for_a_deleted_widget_is_skipped(app):
    from PySide6.QtWidgets import QLabel

    from egx_advisor.desktop.workers import run_async

    label, got = QLabel(), []
    run_async(lambda: time.sleep(0.2) or "late", lambda r, e: got.append(r), owner=label)
    label.deleteLater()
    # processEvents() leaves deferred deletes queued; the app's event loop runs them.
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    wait_for(app, lambda: False, timeout=0.6)
    assert got == [], "the result of a deleted widget's job must not reach it"
    other = QLabel()
    run_async(lambda: "on time", lambda r, e: got.append(r), owner=other)
    assert wait_for(app, lambda: got == ["on time"])

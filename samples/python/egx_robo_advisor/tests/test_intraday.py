"""Prices read from the Thndr X page during the session."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest

from egx_advisor import curriculum as cur
from egx_advisor import paper as pp
from egx_advisor.marketdata import intraday as ir
from egx_advisor.memory import Memory

REF = {"COMI.CA": 128.0, "ETEL.CA": 41.0, "HRHO.CA": 25.0}

WATCHLIST = """
Watchlist
COMI
Commercial International Bank
129.45
+1.44 (1.12%)
Vol 3,655,654
ETEL
Telecom Egypt
-0.35 (-0.85%)
40.65
HRHO
EFG Holding
2,540
25.10
SWDY
Elsewedy Electric
60.20
"""


def test_a_watchlist_page_gives_each_known_stocks_price():
    prices = ir.parse_prices(WATCHLIST, REF)
    # Signed changes, percentages and volumes are skipped; 2,540 is outside the band.
    assert prices == {"COMI.CA": 129.45, "ETEL.CA": 40.65, "HRHO.CA": 25.10}


def test_a_number_far_from_the_last_close_is_not_taken_for_a_price():
    assert ir.parse_prices("COMI\n12.9\n1290\n", REF) == {}
    assert ir.parse_prices("COMI ١٢٩٫٥٠", REF) == {"COMI.CA": 129.5}
    assert ir.parse_prices("", REF) == {}
    assert ir.parse_prices("COMIX 128", REF) == {}, "a longer word is not the ticker"


def test_the_store_keeps_a_price_a_minute_and_counts_full_sessions(tmp_path):
    store = ir.IntradayStore(tmp_path / "intraday.db")
    start = datetime(2026, 10, 1, 10, 0)
    for i in range(ir.SESSION_MINUTES):
        store.add({"COMI.CA": 128 + i / 100}, start + timedelta(minutes=i, seconds=30))
    store.add({"COMI.CA": 200.0}, start + timedelta(seconds=50))   # same minute: replaced
    assert len(store.ticks("COMI.CA")) == ir.SESSION_MINUTES
    assert store.ticks("COMI.CA")[0].price == 200.0
    assert store.sessions() == [date(2026, 10, 1)]
    now = start + timedelta(minutes=ir.SESSION_MINUTES)
    assert store.last("COMI.CA", now).price == pytest.approx(128.59)
    assert store.last("COMI.CA", now + timedelta(hours=2)) is None, "stale is not now"
    # A session in which nothing moved (a holiday's frozen page) does not count.
    for i in range(ir.SESSION_MINUTES):
        store.add({"ETEL.CA": 41.0}, datetime(2026, 10, 4, 10, 0) + timedelta(minutes=i))
    assert store.sessions() == [date(2026, 10, 1)]
    assert store.days() == [date(2026, 10, 1), date(2026, 10, 4)]


def test_reference_closes_come_from_the_archive_before_today():
    @dataclass
    class Row:
        day: date
        close: float

    class Archive:
        def series(self, symbol, since=None):
            if symbol == "BAD.CA":
                raise ValueError("no")
            return [Row(date(2026, 9, 29), 127.0), Row(date(2026, 9, 30), 128.0),
                    Row(date(2026, 10, 1), 999.0)]

    refs = ir.reference_closes(Archive(), ["COMI.CA", "BAD.CA"], date(2026, 10, 1))
    assert refs == {"COMI.CA": 128.0}


def _book_with_trade(tmp_path):
    book = pp.PaperBook(tmp_path / "memory.db", capital=100_000)
    book.open_trade("COMI.CA", 10.0, 9.0, 12.0, source="plan", today=date(2026, 9, 30))
    return book


def test_a_paper_trade_closes_the_same_day_from_prices_read_in_the_session(tmp_path):
    store = ir.IntradayStore(tmp_path / "intraday.db")
    store.add({"COMI.CA": 9.5}, datetime(2026, 9, 30, 13, 0))     # the opening day: ignored
    store.add({"COMI.CA": 10.4}, datetime(2026, 10, 1, 10, 5))
    store.add({"COMI.CA": 8.9}, datetime(2026, 10, 1, 11, 0))     # through the stop
    book = _book_with_trade(tmp_path)
    closed, _ = book.settle(lambda s: [], date(2026, 10, 1), store.ticks)
    (t,) = book.trades("closed")
    assert closed and t.exit == 8.9 and t.exit_reason == "stop" and t.closed == date(2026, 10, 1)


def test_a_target_from_session_prices_exits_at_the_target_not_better(tmp_path):
    store = ir.IntradayStore(tmp_path / "intraday.db")
    store.add({"COMI.CA": 12.6}, datetime(2026, 10, 1, 10, 30))
    book = _book_with_trade(tmp_path)
    book.settle(lambda s: [], date(2026, 10, 1), store.ticks)
    (t,) = book.trades("closed")
    assert t.exit == 12.0 and t.exit_reason == "target"


def test_session_prices_mark_an_open_trade(tmp_path):
    store = ir.IntradayStore(tmp_path / "intraday.db")
    store.add({"COMI.CA": 10.5}, datetime(2026, 10, 1, 10, 30))
    book = _book_with_trade(tmp_path)
    book.settle(lambda s: [], date(2026, 10, 1), store.ticks)
    assert book.trades("open") and book.equity_curve()[-1][1] == pytest.approx(
        100_000 + book.trades("open")[0].pnl(10.5))


def test_the_analyst_reports_a_fresh_live_price(tmp_path):
    from egx_advisor.analyst.tools import Toolbox
    from egx_advisor.bus import StateBus

    store = ir.IntradayStore(tmp_path / "intraday.db")
    box = Toolbox(bus=StateBus(tmp_path / "s.db"), history=lambda s, d: {}, intraday=store)
    assert box._live("COMI.CA") is None
    store.add({"COMI.CA": 129.45}, ir.cairo_now())
    live = box._live("COMI.CA")
    assert live["price"] == 129.45 and "Thndr X" in live["source"]


def test_five_full_sessions_tick_the_intraday_checkpoint(tmp_path):
    memory = Memory(tmp_path / "memory.db")
    store = ir.IntradayStore(tmp_path / "intraday.db")
    for day in range(5):
        for i in range(ir.SESSION_MINUTES):
            store.add({"COMI.CA": 128 + i / 100},
                      datetime(2026, 10, 4 + day, 10, 0) + timedelta(minutes=i))
    s = {x.checkpoint.key: x for x in cur.evaluate(cur.gather(memory, config_dir=tmp_path))}
    assert s["intraday"].state == "done"

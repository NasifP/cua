"""The lab leaves out a stock with bad data instead of refusing to run."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

pytest.importorskip("PySide6.QtWidgets")

from egx_advisor.backtest.lab import run_lab  # noqa: E402
from egx_advisor.clock import TradingCalendar  # noqa: E402
from egx_advisor.marketdata import YahooRow  # noqa: E402
from egx_advisor.strategy.filters import BuyFilter  # noqa: E402
from egx_advisor.strategy.policy import AllocationPolicy  # noqa: E402


def _rows(symbols, sessions=400):
    calendar = TradingCalendar()
    days, day = [], date(2024, 1, 7)
    while len(days) < sessions:
        if calendar.is_session_day(day):
            days.append(day)
        day += timedelta(days=1)
    random.seed(7)
    out = {}
    for symbol in symbols:
        price, series = 50.0, []
        for d in days:
            price *= 1 + random.uniform(-0.015, 0.016)
            p = Decimal(str(round(price, 2)))
            series.append(YahooRow(d, p, p * Decimal("1.01"), p * Decimal("0.99"), p, Decimal(10000)))
        out[symbol] = series
    return out


def test_bad_and_missing_stocks_are_left_out_and_named(tmp_path, monkeypatch):
    from egx_advisor import marketdata
    from egx_advisor.desktop import lab_tab

    symbols = [i.symbol for i in AllocationPolicy().universe]
    rows = _rows(symbols)
    bad, missing = symbols[0], symbols[1]
    first = rows[bad][100]
    rows[bad][100] = YahooRow(first.day, first.open, first.high, first.low,
                              first.high * Decimal("1.2"), first.volume)
    rows[missing] = []

    class FakeYahoo:
        def __init__(self, **kwargs):
            pass

        async def history(self, universe, days):
            return rows

    monkeypatch.setattr(marketdata, "YahooMarketData", FakeYahoo)
    monkeypatch.setattr(lab_tab, "HISTORY_CACHE", tmp_path)
    history, excluded = lab_tab.load_history(2, synthetic=False)
    assert set(excluded) == {bad, missing}
    assert "CLOSE_OUTSIDE_RANGE" in excluded[bad] and "EMPTY_HISTORY" in excluded[missing]

    report = run_lab(history, [BuyFilter("rsi", {"days": 14, "above": 70})])
    assert report.sessions > 300, "the lab runs on the stocks that are left"

    # The cache is checked again on the next run.
    _history, again = lab_tab.load_history(2, synthetic=False)
    assert set(again) == {bad, missing}

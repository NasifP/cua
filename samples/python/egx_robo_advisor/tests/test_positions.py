"""Profit/loss inputs, stops and targets, and the average calculator."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from egx_advisor import average, levels
from egx_advisor.assistant import Assistant
from egx_advisor.bus import StateBus
from egx_advisor.execution.thndr import PortfolioReadError, _parse_portfolio
from egx_advisor.marketdata import YahooRow

D = Decimal


def _bars(n=80, start=100.0, step=0.5, spread=2.0):
    rows, price = [], start
    for i in range(n):
        price += step
        rows.append(YahooRow(date(2026, 1, 1) + timedelta(days=i), D(str(price)),
                             D(str(price + spread / 2)), D(str(price - spread / 2)),
                             D(str(price)), D(1000)))
    return rows


# ------------------------------------------------------------------ avg cost


def test_average_cost_is_read_when_the_table_shows_it():
    portfolio = _parse_portfolio({"positions": [
        {"symbol": "COMI.CA", "quantity": "100", "market_value": "8500", "avg_cost": "70.10"},
        {"symbol": "TMGH.CA", "quantity": "10", "market_value": "500"},
        {"symbol": "ETEL.CA", "quantity": "10", "market_value": "500", "avg_cost": "n/a"},
    ], "cash_egp": "0"}, demo_confirmed=False)
    assert portfolio.positions["COMI.CA"].avg_cost == D("70.10")
    assert portfolio.positions["TMGH.CA"].avg_cost is None
    assert portfolio.positions["ETEL.CA"].avg_cost is None, "unreadable cost: blank, not a failure"


def test_a_bad_quantity_still_fails_the_read():
    with pytest.raises(PortfolioReadError):
        _parse_portfolio({"positions": [{"symbol": "COMI.CA", "quantity": "x",
                                         "market_value": "1"}]}, demo_confirmed=False)


# ------------------------------------------------------------------ levels


def test_atr_is_the_average_true_range():
    assert levels.atr(_bars(20, spread=2.0)) == pytest.approx(2.0)
    assert levels.atr(_bars(10)) is None


def test_levels_sit_below_and_above_the_price_by_style():
    rows = _bars()
    price = float(rows[-1].close)
    swing = levels.compute("COMI.CA", rows, price, "swing")
    long_ = levels.compute("COMI.CA", rows, price, "long")
    assert swing.stop < price < swing.target1 < swing.target2
    assert long_.stop < swing.stop, "a long-term investor gives the stock more room"
    assert long_.target2 > swing.target2


def test_status_warns_near_and_below_the_stop():
    base = levels.Levels("X", price=100, atr=2, stop=90, target1=110, target2=120)
    assert base.status == "ok"
    assert levels.Levels("X", 91.5, 2, 90, 110, 120).status == "near_stop"
    assert levels.Levels("X", 89, 2, 90, 110, 120).status == "below_stop"
    assert levels.Levels("X", 111, 2, 90, 110, 120).status == "above_target1"
    assert levels.Levels("X", 121, 2, 90, 110, 120).status == "above_target2"
    assert levels.Levels("X", 105, 2, 90, 110, 120).position == pytest.approx(0.5)


def test_levels_for_a_portfolio_skip_what_they_cannot_price():
    rows = _bars()
    out = levels.for_portfolio([
        {"symbol": "COMI.CA", "quantity": "10", "market_value": "1400", "avg_cost": "120"},
        {"symbol": "NEW.CA", "quantity": "10", "market_value": "100"},
        {"symbol": "ZERO.CA", "quantity": "0", "market_value": "0"},
    ], {"COMI.CA": rows}, "swing")
    assert list(out) == ["COMI.CA"]
    assert out["COMI.CA"]["price"] == 140.0 and out["COMI.CA"]["avg_cost"] == 120.0


def test_style_comes_from_settings_with_a_safe_default():
    assert levels.style_from({"EGX_STYLE": "long"}) == "long"
    assert levels.style_from({"EGX_STYLE": "yolo"}) == "swing"
    assert levels.style_from({}) == "swing"


# ------------------------------------------------------------------ average


def test_shares_needed_to_reach_a_target_average():
    r = average.shares_needed(100, 85, 80, 75)
    assert (r.buy, r.new_quantity, r.new_average) == (100, 200, 80.0)
    up = average.shares_needed(100, 50, 55, 60)
    assert up.buy == 100 and up.new_average == 55.0
    rounded = average.shares_needed(100, 74.42, 70, 65)
    assert rounded.buy == 89 and rounded.new_average <= 70
    with pytest.raises(ValueError):
        average.shares_needed(100, 85, 70, 75)  # cannot pass the buying price


@pytest.mark.parametrize(("question", "expected"), [
    ("معايا 100 سهم متوسط 85 عايز أوصل 80 بسعر 75", (100, 85, 80, 75)),
    ("معايا ١٠٠ سهم بمتوسط ٧٤٫٤٢ عايز أنزل ل 70 بسعر 65", (100, 74.42, 70, 65)),
    ("I have 200 shares, average 50, want to get to 45, price 40", (200, 50, 45, 40)),
    ("ليه المتوسط المتحرك مهم؟", None),
    ("what is my average?", None),
])
def test_average_requests_are_recognised(question, expected):
    assert average.average_request(question) == expected


def test_the_chat_answers_the_calculator_without_the_model(tmp_path):
    assistant = Assistant(bus=StateBus(tmp_path / "s.db"),
                          completion=lambda **_: pytest.fail("model called"))
    answer = assistant.answer("معايا 100 سهم متوسط 85 عايز أوصل 80 بسعر 75")
    assert "100 سهم" in answer and "80.00" in answer and "مش نصيحة" in answer

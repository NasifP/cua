"""The operator's indicators: arithmetic, signals, readings and the EGX study."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from egx_advisor import indicators as ind


@dataclass
class Row:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def rows(closes, volumes=None):
    start = date(2023, 1, 1)
    volumes = volumes or [1000.0] * len(closes)
    return [Row(start + timedelta(days=i), c, c + 1, c - 1, c, v)
            for i, (c, v) in enumerate(zip(closes, volumes))]


def wave(n=400, period=40, base=100.0, amp=10.0, drift=0.0):
    return [base + drift * i + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


# --------------------------------------------------------------------------- #
# Arithmetic
# --------------------------------------------------------------------------- #


def test_moving_averages():
    assert ind.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    e = ind.ema([1, 2, 3, 4], 2)
    assert e[:2] == [None, 1.5] and e[2] == pytest.approx(2.5) and e[3] == pytest.approx(3.5)


def test_rsi_is_100_when_only_rising_and_low_when_falling():
    assert ind.rsi(list(range(1, 30)), 14)[-1] == 100.0
    assert ind.rsi(list(range(30, 1, -1)), 14)[-1] == pytest.approx(0.0)
    assert ind.rsi([1, 2], 14) == [None, None]


def test_macd_and_bands_line_up_with_the_prices():
    closes = wave()
    line, signal = ind.macd(closes)
    assert len(line) == len(signal) == len(closes)
    assert line[24] is None and line[25] is not None and signal[33] is not None
    mid, upper, lower = ind.bollinger(closes, 20, 2)
    assert all(lo < m < up for m, up, lo in zip(mid[19:], upper[19:], lower[19:]))


def test_stochastic_stays_between_0_and_100():
    r = rows(wave())
    b = ind.Bars.of(r)
    k, d = ind.stochastic(b.highs, b.lows, b.closes)
    assert all(0 <= x <= 100 for x in k if x is not None)
    assert len(d) == len(k)


# --------------------------------------------------------------------------- #
# The set
# --------------------------------------------------------------------------- #


def test_the_set_round_trips_and_fills_in_missing_indicators():
    chosen = [ind.Choice("rsi", True, {"length": 9, "low": 25, "high": 75})]
    loaded = ind.load_set(ind.dump_set(chosen))
    assert [c.key for c in loaded] == list(ind.CATALOG)
    rsi = next(c for c in loaded if c.key == "rsi")
    assert rsi.on and rsi.value("length") == 9 and rsi.label() == "rsi(9, 25, 75)"
    assert not next(c for c in loaded if c.key == "macd").on


def test_a_broken_saved_set_falls_back_to_defaults():
    assert [c.on for c in ind.load_set("not json")] == [c.on for c in ind.default_set()]
    loaded = ind.load_set('[{"key": "sma", "on": true, "params": {"length": "x"}}]')
    assert next(c for c in loaded if c.key == "sma").value("length") == 50


@pytest.mark.parametrize(("key", "params"), [
    ("sma", {"length": 1}), ("sma", {"length": 20.5}), ("ema_cross", {"fast": 50, "slow": 20}),
    ("rsi", {"low": 80, "high": 70}), ("bollinger", {"width": 0}), ("rsi", {"length": "abc"}),
])
def test_bad_settings_are_refused(key, params):
    with pytest.raises(ValueError):
        ind.check_params(key, params)


def test_only_chosen_indicators_go_to_the_chart():
    chosen = [ind.Choice("rsi", True), ind.Choice("macd", False), ind.Choice("sma", True)]
    assert ind.tv_studies(chosen) == ["RSI@tv-basicstudies", "MASimple@tv-basicstudies"]


# --------------------------------------------------------------------------- #
# Signals, readings, study
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", list(ind.CATALOG))
def test_every_indicator_gives_signals_and_a_reading_on_a_wave(key):
    volumes = [3000.0 if i % 40 == 5 else 1000.0 for i in range(400)]
    r = rows(wave(), volumes)
    choice = ind.Choice(key, True, dict(ind.CATALOG[key].params))
    found = ind.signals(choice, ind.Bars.of(r))
    assert found, key
    assert {side for _, side in found} <= {"buy", "sell"}
    reading = ind.reading(choice, ind.Bars.of(r))
    assert reading["indicator"].startswith(key) and "last_signal" in reading


def test_signals_never_use_later_bars():
    closes = wave()
    full = ind.signals(ind.Choice("macd", True), ind.Bars.of(rows(closes)))
    cut = ind.signals(ind.Choice("macd", True), ind.Bars.of(rows(closes[:300])))
    assert cut == [s for s in full if s[0] < 300]


def test_rsi_buys_come_after_lows_on_a_wave_and_the_study_says_it_helped():
    # A regular wave: RSI rising back through 30 happens near the bottoms, so
    # the next 20 sessions rise; the study must see that.
    history = {f"S{i}.CA": rows(wave(n=800, period=40, amp=8 + i)) for i in range(4)}
    result = ind.study(ind.Choice("rsi", True, {"length": 5, "low": 30, "high": 70}), history)
    h = result["h20"]
    assert h["buy_signals"] >= ind.MIN_SIGNALS
    assert h["buy_avg_pct"] > h["market_avg_pct"]
    assert result["verdict"] == "helped"
    assert result["best_symbols"] and result["symbols"] == 4


def test_a_study_with_few_signals_says_so():
    history = {"A.CA": rows([100 + i * 0.1 for i in range(200)])}
    assert ind.study(ind.Choice("rsi", True), history)["verdict"] == "too_few"


def test_verdicts():
    def result(n, edge, hit=60, market_hit=50):
        return {"h20": {"buy_signals": n, "buy_edge_pct": edge, "buy_hit_pct": hit,
                        "market_hit_pct": market_hit}}
    assert ind.verdict(result(5, 5)) == "too_few"
    assert ind.verdict(result(50, 2.0)) == "helped"
    assert ind.verdict(result(50, 2.0, hit=40)) == "no_edge"
    assert ind.verdict(result(50, -2.0)) == "misled"
    assert ind.verdict(result(50, 0.3)) == "no_edge"

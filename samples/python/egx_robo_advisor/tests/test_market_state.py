"""Sprint 3: regime, dynamic weights, liquidity sizing, macro risk-off, sectors."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from egx_advisor import levels, market_state as ms, scanner, sectors
from egx_advisor.analyst import committee as cm
from egx_advisor.analyst.tools import Toolbox
from egx_advisor.bus import StateBus
from egx_advisor.marketdata.yahoo import YahooRow


def _rows(closes, volume=1000):
    end = date.today() - timedelta(days=1)
    n = len(closes)
    return [YahooRow(end - timedelta(days=n - 1 - i), D(str(c)), D(str(c + 1)),
                     D(str(c - 1)), D(str(c)), D(volume)) for i, c in enumerate(closes)]


UP = [50 + i * 0.5 for i in range(120)]
DOWN = [110 - i * 0.5 for i in range(120)]
FLAT = [60 + (1.5 if i % 2 else -1.5) for i in range(120)]


# ------------------------------------------------------------------ regime


@pytest.mark.parametrize(("closes", "regime"), [
    (UP, ms.Regime.UPTREND), (DOWN, ms.Regime.DOWNTREND), (FLAT, ms.Regime.SIDEWAYS),
    (UP[:30], ms.Regime.UNKNOWN)])
def test_regime_is_read_from_prices(closes, regime):
    assert ms.detect_market_regime(_rows(closes)).regime is regime
    assert ms.detect_market_regime(closes).regime is regime  # plain numbers work too


def test_sideways_boosts_oscillators_and_a_trend_boosts_trend_followers():
    readings = [{"indicator": "rsi(14, 30, 70)", "zone": "oversold"},
                {"indicator": "macd(12, 26, 9)", "above_signal": False}]
    side = ms.weigh_readings(readings, ms.Regime.SIDEWAYS)
    up = ms.weigh_readings(readings, ms.Regime.UPTREND)
    assert side["combined_score"] > 0 > up["combined_score"]
    assert side["indicators"][0]["weight"] == 1.5 and up["indicators"][1]["weight"] == 1.5


def test_the_operators_study_still_weighs_on_top_of_the_regime():
    readings = [{"indicator": "rsi(14, 30, 70)", "zone": "oversold"}]
    misled = ms.weigh_readings(readings, ms.Regime.SIDEWAYS, {"rsi(14, 30, 70)": "misled"})
    assert misled["indicators"][0]["weight"] == 0.75


# ------------------------------------------------------------------ liquidity


def test_a_buy_is_capped_at_a_share_of_average_daily_volume():
    cap = ms.liquidity_cap(_rows(UP, volume=10_000), env={"EGX_ADTV_PCT": "2"})
    assert cap.max_shares == 200 and cap.cap(5_000) == (200, True) and cap.cap(50) == (50, False)


def test_unknown_volume_fails_closed_and_the_setting_is_clamped():
    assert ms.liquidity_cap(_rows(UP[:5])).cap(10) == (0, True)
    assert ms.adtv_pct({"EGX_ADTV_PCT": "90"}) == 10.0
    assert ms.adtv_pct({"EGX_ADTV_PCT": "nan"}) == ms.DEFAULT_ADTV_PCT


def test_the_scan_caps_share_counts_by_liquidity():
    flags = dict.fromkeys(("trend", "momentum", "volume", "volatility"), True)
    o = scanner.Opportunity("COMI.CA", 10.0, 1, 1, 1, 100, 1, 50, flags, adtv=1_000)
    assert o.buys(1_000_000, env={"EGX_ADTV_PCT": "3"}) == (30, True)


# ------------------------------------------------------------------ risk-off


def test_macro_risk_off_on_a_hard_egx30_fall_or_a_news_halt():
    assert ms.macro_risk_off([100, 100, 100, 100, 100, 95]).risk_off  # -5% in a session
    assert ms.macro_risk_off([100, 99, 98, 96, 94, 92.5]).risk_off   # -7.5% in five
    assert ms.macro_risk_off(UP, {"risk_state": "buys_halted"}).risk_off
    assert not ms.macro_risk_off(UP, {"risk_state": "risk_on"}).risk_off


def test_risk_off_tightens_the_chandelier_stop():
    rows = _rows(UP)
    calm = levels.compute("X", rows, UP[-1], "swing")
    tight = levels.compute("X", rows, UP[-1], "swing", risk_off=True)
    assert tight.stop > calm.stop and tight.stop_atr == 1.25 and tight.to_json()[
        "risk_off_tightened"]
    assert levels.stop_multiple("trader", True) == 1.0


# ------------------------------------------------------------------ sectors


def test_every_scanned_stock_has_a_sector_and_dominance_is_flagged():
    assert sectors.sector_of("COMI") == sectors.BANKS
    flag = sectors.sector_momentum(["TMGH.CA", "PHDC.CA", "COMI.CA", "OCDI.CA"])
    assert flag["sector"] == sectors.REAL_ESTATE and flag["count"] == 3
    assert sectors.sector_momentum(["TMGH.CA", "COMI.CA", "ETEL.CA"]) is None


# ------------------------------------------------------------------ tools and committee


@pytest.fixture
def bus(tmp_path):
    bus = StateBus(tmp_path / "s.db")
    bus.put("portfolio", {"as_of": "2026-09-27", "total_value": "1000000",
                          "cash_egp": "1000000", "positions": []})
    bus.resume(actor="test")
    return bus


def _toolbox(bus, egx30=UP, volume=1000):
    def history(symbols, days):
        # Many symbols: the scan list, for the EGX 30 stand-in; one: the stock asked about.
        path = egx30 if len(symbols) > 1 else UP
        return {s: _rows(path, volume) for s in symbols}
    return Toolbox(bus=bus, history=history, env={"EGX_ADTV_PCT": "3"})


def test_prepare_buy_refuses_more_than_the_liquidity_limit_whatever_the_cash(bus):
    box = _toolbox(bus)
    last = UP[-1]
    refused = box.prepare_buy("COMI", 100, last)  # 3% of 1,000 = 30 shares
    assert refused["prepared"] is False and "average daily volume" in refused["refused"]
    assert box.prepare_buy("COMI", 30, last)["prepared"] is True


def test_the_risk_tools_show_regime_liquidity_and_risk_off(bus):
    bus.put("regime", {"risk_state": "buys_halted", "drivers": ["CBE surprise"]})
    box = _toolbox(bus)
    out = box.stock_levels("COMI")
    assert out["liquidity"]["max_shares"] == 30 and out["regime"]["regime"] == "uptrend"
    assert out["macro"]["MACRO_RISK_OFF"] and "تخفيف المراكز" in out["risk_off_action"]
    assert out["levels"]["risk_off_tightened"]
    capped = box.average_calculator(100, 90, 70, 60, symbol="COMI")  # needs 200
    assert capped["buy"] == 30 and capped["capped_by_liquidity"]


def test_the_technical_view_carries_weighted_indicators(bus):
    view = _toolbox(bus).analyze_stock("COMI")
    assert view["regime"]["regime"] == "uptrend"
    assert view["weighted_indicators"]["regime"] == "uptrend"


def test_risk_off_closes_the_buy_gate_and_the_briefing_advises_trimming():
    good = {m.key: cm.parse_report(m, t) for m, t in zip(cm.MEMBERS, (
        "VERDICT: positive", "VERDICT: positive\nBRAKE: no", "RISK: allow\nMAX_EGP: none"))}
    macro = ms.macro_risk_off([100, 100, 100, 100, 100, 95])
    gate = cm.decide(good, macro)
    assert not gate.buy_allowed and any("MACRO_RISK_OFF" in r for r in gate.reasons)
    text = cm.briefing(good, gate, macro, {"sector": "Banks", "count": 3, "of": 5,
                                           "symbols": ["COMI.CA", "CIEB.CA", "ADIB.CA"]})
    assert "تخفيف المراكز" in text and "زخم القطاعات" in text


def test_zero_prices_are_skipped_not_a_crash():
    closes = [D("10")] * 150 + [D("0")] * 30
    assert scanner.analyze("X.CA", closes, [D(1000)] * 180) is None


def test_the_macro_check_is_fetched_once_across_specialist_threads(bus):
    import threading as th

    fetched = []

    def history(symbols, days):
        fetched.append(tuple(symbols))
        return {s: _rows(UP) for s in symbols}

    box = Toolbox(bus=bus, history=history)
    workers = [th.Thread(target=box.macro) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert sum(1 for f in fetched if len(f) > 1) == 1, "one scan-list fetch for the stand-in"


def test_the_egx30_stand_in_averages_the_scan_lists_daily_moves():
    from datetime import date, timedelta

    from egx_advisor.marketdata import index_proxy

    class Bar:
        def __init__(self, day, close):
            self.day, self.close = day, close

    d = date(2026, 9, 1)
    days = [d + timedelta(i) for i in range(4)]
    rows = {
        "A.CA": [Bar(x, c) for x, c in zip(days, (10, 11, 11, 11))],     # +10%, 0, 0
        "B.CA": [Bar(x, c) for x, c in zip(days, (20, 20, 18, 18))],     # 0, -10%, 0
        "C.CA": [Bar(x, c) for x, c in zip(days, (5, 5, 5, 50))],        # 0, 0, +900% bad print
        "D.CA": [Bar(days[3], 7)],                                       # listed late
    }
    bars = index_proxy.build(rows)
    assert [b.day for b in bars] == days[1:]
    levels = [b.close for b in bars]
    assert levels[0] == pytest.approx(1000 * (1 + 0.10 / 3))
    assert levels[1] == pytest.approx(levels[0] * (1 - 0.10 / 3))
    # The bad print is held to +20%, not +900%.
    assert levels[2] == pytest.approx(levels[1] * (1 + 0.20 / 3))
    # A day when fewer than half of the listed stocks traded does not count.
    thin = {"A.CA": rows["A.CA"], "B.CA": rows["B.CA"][:1], "C.CA": rows["C.CA"][:1]}
    assert [b.day for b in index_proxy.build(thin)] == []
    assert index_proxy.build({"Z.CA": [Bar(d, 0), Bar(days[1], 5)]}) == []


def test_the_market_overview_reports_the_stand_in_and_a_fallback_dollar_rate(bus):
    box = _toolbox(bus)
    box.usd_egp = lambda: 51.85
    out = box.market_overview()
    assert "stand-in" in out["egx30"]["source"] and out["egx30"]["level"] > 0
    assert out["egx30_regime"]["regime"] == "uptrend"
    assert out["usd_egp"] == 51.85 and "Yahoo" in out["usd_egp_source"]
    bus.put("fx", {"usd_egp": "52.10"})
    assert box.market_overview()["usd_egp"] == "52.10", "Thndr X's own rate comes first"


def test_one_stock_from_yahoo_is_read_from_its_nested_columns(monkeypatch):
    pd = pytest.importorskip("pandas")
    import sys
    import types

    from egx_advisor.marketdata.yahoo import _yfinance_fetch

    index = pd.to_datetime(["2026-09-28", "2026-09-29"])

    def download(symbols, **kw):
        cols = pd.MultiIndex.from_product([symbols, ["Open", "High", "Low", "Close", "Volume"]])
        return pd.DataFrame([[1, 2, 0.5, 1.5, 100] * len(symbols)] * 2, index=index,
                            columns=cols)

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    one = _yfinance_fetch(["COMI.CA"], 30)
    assert [r.close for r in one["COMI.CA"]] == [D("1.5"), D("1.5")]
    two = _yfinance_fetch(["COMI.CA", "ETEL.CA"], 30)
    assert set(two) == {"COMI.CA", "ETEL.CA"}

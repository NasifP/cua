"""The paper trading journal and the loss limits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from egx_advisor import paper as pp
from egx_advisor import risk_limits as rl

DAY = date(2026, 9, 1)


@dataclass
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float


def book(tmp_path: Path, **kw) -> pp.PaperBook:
    return pp.PaperBook(tmp_path / "memory.db", **kw)


# --------------------------------------------------------------------------- limits


def test_limits_come_from_settings_within_their_ranges():
    assert rl.limits_from({}) == rl.Limits(2.0, 10.0)
    assert rl.limits_from({"EGX_DAILY_LOSS_PCT": "3", "EGX_MAX_DRAWDOWN_PCT": "15"}) == \
        rl.Limits(3.0, 15.0)
    wild = rl.limits_from({"EGX_DAILY_LOSS_PCT": "0", "EGX_MAX_DRAWDOWN_PCT": "nan"})
    assert wild == rl.Limits(0.5, 10.0)
    assert rl.risk_per_trade_pct({"EGX_RISK_PER_TRADE_PCT": "50"}) == 5.0


def test_the_daily_limit_fires_once_a_day_from_the_days_first_value():
    limits = rl.Limits(2.0, 50.0)
    state, breach = rl.step(None, 100_000, "d1", limits)
    assert breach is None
    state, breach = rl.step(state, 98_500, "d1", limits)
    assert breach is None
    state, breach = rl.step(state, 97_900, "d1", limits)
    assert breach.kind == "daily" and breach.loss_pct == pytest.approx(2.1)
    assert rl.paused(state, "d1") == "daily"
    state, breach = rl.step(state, 97_000, "d1", limits)
    assert breach is None, "once a day"
    state, breach = rl.step(state, 97_000, "d2", limits)
    assert breach is None and rl.paused(state, "d2") is None  # a new day, a new start


def test_the_drawdown_fires_until_resumed_and_resuming_accepts_the_loss():
    limits = rl.Limits(20.0, 10.0)
    state, _ = rl.step(None, 100_000, "d1", limits)
    state, _ = rl.step(state, 110_000, "d2", limits)
    state, breach = rl.step(state, 98_000, "d3", limits)
    assert breach.kind == "drawdown" and rl.paused(state, "d9") == "drawdown"
    state, breach = rl.step(state, 97_000, "d4", limits)
    assert breach is None and rl.paused(state, "d4") == "drawdown"
    state, breach = rl.step(state, 97_000, "d4", limits, resumed=True)
    assert breach is None and rl.paused(state, "d4") is None and state["peak"] == 97_000


def test_no_account_value_judges_nothing():
    state, breach = rl.step({"day": "d1", "start": 100.0}, 0, "d1", rl.Limits())
    assert breach is None and state["start"] == 100.0


# --------------------------------------------------------------------------- journal


def test_shares_are_sized_from_the_risk_and_capped_by_the_cash(tmp_path):
    b = book(tmp_path, capital=100_000, risk_pct=1.0)
    t = b.open_trade("comi", 10.0, 9.0, 12.0, source="you", today=DAY)
    assert t.symbol == "COMI.CA" or t.symbol == "COMI"
    assert t.quantity == 1000 and t.risk == pytest.approx(1000)
    assert t.fees_in == pytest.approx(pp.costs(10.0, 1000)) and t.fees_in > 0
    # A very tight stop would size far past the cash: the cash caps it.
    tight = b.open_trade("ETEL", 10.0, 9.99, 11.0, source="you", today=DAY)
    assert tight.quantity * 10.0 < 100_000 - 10_000


def test_bad_or_repeated_trades_are_refused(tmp_path):
    b = book(tmp_path)
    for entry, stop, target in ((10, 11, 12), (10, 9, 9.5), (10, 0, 12), (float("nan"), 9, 12)):
        with pytest.raises(ValueError):
            b.open_trade("COMI", entry, stop, target, source="you", today=DAY)
    b.open_trade("COMI", 10, 9, 12, source="you", today=DAY)
    with pytest.raises(ValueError, match="already"):
        b.open_trade("COMI", 10, 9, 12, source="plan", today=DAY)


def _settled(tmp_path, bars):
    b = book(tmp_path, capital=100_000, risk_pct=1.0)
    b.open_trade("COMI", 10.0, 9.0, 12.0, source="plan", today=DAY)
    closed, _ = b.settle(lambda s: bars, DAY + timedelta(days=10))
    return b, closed


@pytest.mark.parametrize("bars, exit_, why", [
    ([Bar(DAY + timedelta(1), 10, 10.5, 9.5, 10.2), Bar(DAY + timedelta(2), 10, 10.1, 8.8, 9)],
     9.0, "stop"),
    ([Bar(DAY + timedelta(1), 8.5, 8.9, 8.0, 8.2)], 8.5, "stop"),              # gap under
    ([Bar(DAY + timedelta(1), 10.5, 12.4, 10.4, 12.1)], 12.0, "target"),
    ([Bar(DAY + timedelta(1), 12.5, 13, 12.3, 12.8)], 12.5, "target"),        # gap over
    ([Bar(DAY + timedelta(1), 10, 12.5, 8.5, 11)], 9.0, "stop"),               # both: the worse
])
def test_trades_close_at_the_first_session_reaching_the_stop_or_target(tmp_path, bars,
                                                                         exit_, why):
    b, closed = _settled(tmp_path, bars)
    (t,) = b.trades("closed")
    assert len(closed) == 1 and t.exit == exit_ and t.exit_reason == why
    expected = (exit_ - 10.0) * 1000 - pp.costs(10.0, 1000) - pp.costs(exit_, 1000)
    assert t.pnl() == pytest.approx(expected)


def test_the_opening_session_itself_is_not_used_and_open_trades_are_marked(tmp_path):
    b, closed = _settled(tmp_path, [Bar(DAY, 10, 13, 5, 10),
                                    Bar(DAY + timedelta(1), 10, 10.8, 9.6, 10.5)])
    assert not closed and len(b.trades("open")) == 1
    (day, equity), = b.equity_curve()
    assert equity == pytest.approx(100_000 + 500 - pp.costs(10.0, 1000))


def test_closing_by_hand_counts_as_manual(tmp_path):
    b = book(tmp_path)
    t = b.open_trade("COMI", 10, 9, 12, source="you", today=DAY)
    b.close_by_hand(t.id, 10.5, DAY + timedelta(3))
    (closed,) = b.trades("closed")
    assert closed.exit_reason == "manual"
    assert b.stats().rule_exits == 0 and b.stats().closed == 1


def test_a_daily_loss_pauses_new_paper_trades_for_the_day(tmp_path):
    b = book(tmp_path, capital=100_000, risk_pct=5.0, limits=rl.Limits(2.0, 50.0))
    b.settle(lambda s: [], DAY)                       # the day's first value: 100,000
    b.open_trade("COMI", 10.0, 9.0, 12.0, source="you", today=DAY)
    later = DAY + timedelta(1)
    b.settle(lambda s: [], later)
    _, breach = b.settle(lambda s: [Bar(later, 9.5, 9.6, 8.5, 8.6)], later)
    assert breach is not None and breach.kind == "daily"
    assert b.paused(later) == "daily"
    with pytest.raises(ValueError, match="paused"):
        b.open_trade("ETEL", 20.0, 19.0, 23.0, source="you", today=later)
    assert b.paused(later + timedelta(1)) is None


# --------------------------------------------------------------------------- agent


class _Portfolio:
    def __init__(self, value: float) -> None:
        self.total_value = Decimal(str(value))


def test_the_real_account_loss_limit_halts_the_bot_once(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from egx_advisor.bus import StateBus
    from egx_advisor.egx_cua_agent import EgxCuaAgent

    monkeypatch.setenv("EGX_DAILY_LOSS_PCT", "2")
    monkeypatch.setenv("EGX_MAX_DRAWDOWN_PCT", "10")
    bus = StateBus(tmp_path / "state.db")
    bus.resume(actor="test", reason="armed")
    agent = EgxCuaAgent.__new__(EgxCuaAgent)
    agent.bus = bus
    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    assert agent._check_loss_limits(_Portfolio(100_000), now) is False
    assert agent._check_loss_limits(_Portfolio(97_000), now) is True
    control = bus.control_state()
    assert control.halted and control.actor == "loss limit" and "daily loss" in control.reason
    # The operator looks and resumes: the same day's breach does not fire again.
    bus.resume(actor="operator", reason="seen")
    assert agent._check_loss_limits(_Portfolio(96_500), now) is False
    # A drawdown past 10% of the peak halts again; resuming accepts it.
    assert agent._check_loss_limits(_Portfolio(89_000), now) is True
    bus.resume(actor="operator", reason="accepted")
    assert agent._check_loss_limits(_Portfolio(89_000), now) is False
    assert bus.get("loss_guard")["payload"]["peak"] == 89_000


def test_the_real_account_guard_skips_a_read_without_cash(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from egx_advisor.bus import StateBus
    from egx_advisor.egx_cua_agent import EgxCuaAgent

    bus = StateBus(tmp_path / "state.db")
    bus.resume(actor="test", reason="armed")
    agent = EgxCuaAgent.__new__(EgxCuaAgent)
    agent.bus = bus
    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    bus.put("portfolio", {"cash_visible": True})
    assert agent._check_loss_limits(_Portfolio(150_000), now) is False
    # The next page shows no cash: the total drops by the cash, which is no loss.
    bus.put("portfolio", {"cash_visible": False})
    assert agent._check_loss_limits(_Portfolio(100_000), now) is False
    assert not bus.control_state().halted
    assert bus.get("loss_guard")["payload"]["value"] == 150_000

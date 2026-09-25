"""The four-factor strategy: what to hold, how much, and what it never does."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from egx_advisor.strategy import four_factor as ff
from egx_advisor.strategy.policy import AllocationPolicy
from egx_advisor.types import Instrument, Sleeve
from tests.test_agent_pipeline import (
    SESSION_TIME,
    _freeze_session,  # noqa: F401 - autouse: EGX session clock
    _wire_tree,
    build,
)

PARAMS = ff.FourFactorParams()
N = PARAMS.lookback + 5
UNI = tuple(Instrument(f"S{i}.CA", f"S{i}", Sleeve.BLUE_CHIP) for i in range(6))


def prices(step=0.002, wobble=0.0):
    out, p = [], 100.0
    for i in range(N):
        p *= 1 + step + (wobble if i % 2 else -wobble)
        out.append(D(f"{p:.4f}"))
    return out


def volume(recent=1.2):
    return [D(1_000_000)] * (N - 5) + [D(int(1_000_000 * recent))] * 5


def test_a_calm_rising_name_is_bought_and_a_falling_one_is_not():
    d = ff.decide(UNI[:2], {"S0.CA": prices(), "S1.CA": prices(-0.002)},
                  {"S0.CA": volume(), "S1.CA": volume()}, {}, PARAMS)
    assert d.actions == {"S0.CA": "enter", "S1.CA": "out"}
    assert d.targets["S0.CA"] == PARAMS.max_name_weight  # one name: capped
    assert d.targets["S1.CA"] == 0


def test_a_held_name_rides_a_quiet_day_but_leaves_when_the_trend_breaks():
    held = {"S0.CA": D("0.15"), "S1.CA": D("0.15")}
    d = ff.decide(UNI[:2], {"S0.CA": prices(), "S1.CA": prices(-0.002)},
                  {"S0.CA": volume(recent=0.3), "S1.CA": volume()}, held, PARAMS)
    assert d.actions["S0.CA"] == "hold", "thin volume alone must not force a sale"
    assert d.actions["S1.CA"] == "exit" and d.targets["S1.CA"] == 0
    assert "trend" in d.reasons["S1.CA"] and "momentum" in d.reasons["S1.CA"]


def test_missing_prices_keep_a_name_where_it_is_never_a_sell_off():
    held = {"S0.CA": D("0.20"), "S1.CA": D("0.10")}
    d = ff.decide(UNI[:2], {}, {}, held, PARAMS)
    assert d.actions == {"S0.CA": "no data", "S1.CA": "no data"}
    assert d.targets == held


def test_the_calmer_name_gets_more_and_the_total_stays_invested_or_less():
    closes = {f"S{i}.CA": prices(wobble=0.001 * (i + 1)) for i in range(6)}
    vols = {s: volume() for s in closes}
    d = ff.decide(UNI, closes, vols, {}, PARAMS)
    entered = [s for s, a in d.actions.items() if a == "enter"]
    assert len(entered) >= 5
    weights = [d.targets[s] for s in entered]
    assert weights == sorted(weights, reverse=True), "weight falls as volatility rises"
    assert sum(d.targets.values()) <= PARAMS.invested
    assert all(w <= PARAMS.max_name_weight for w in d.targets.values())
    assert d.describe().startswith("FOUR-FACTOR:")


def test_parameters_come_from_the_sanctioned_sets():
    with pytest.raises(ValueError):
        ff.FourFactorParams(trend_days=137)
    with pytest.raises(ValueError):
        ff.FourFactorParams(invested=D("1.00"))


def test_the_plan_sells_an_exit_and_buys_an_entry_with_every_usual_limit():
    from datetime import datetime, timezone

    from egx_advisor.strategy.rebalance import plan_rebalance
    from egx_advisor.types import MarketSnapshot, Portfolio, Position, Quote, Tradability

    now = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
    policy = AllocationPolicy()
    held, fresh = policy.universe[0].symbol, policy.universe[1].symbol
    portfolio = Portfolio(
        as_of=now, cash_egp=D("500000"),
        positions={held: Position(held, D("5000"), D("500000"))},
        demo_confirmed=True,
    )
    market = MarketSnapshot(
        as_of=now,
        quotes={i.symbol: Quote(i.symbol, D("100"), D("100"), now, Tradability.OPEN)
                for i in policy.universe},
        usd_egp=D("50"), usd_egp_lookback=D("48"),
    )
    targets = {i.symbol: D(0) for i in policy.universe}
    targets[fresh] = D("0.20")
    plan = plan_rebalance(portfolio=portfolio, market=market, policy=policy,
                          targets=targets, state="FOUR-FACTOR: test", today=now.date())
    sides = {o.symbol: o.side.value for o in plan.orders}
    assert sides.get(held) == "sell" and plan.policy_state == "FOUR-FACTOR: test"
    assert sum(o.notional for o in plan.orders) <= (
        policy.params.max_turnover_per_cycle * portfolio.total_value)


def test_the_backtester_never_shows_the_strategy_the_day_it_decides(monkeypatch):
    from egx_advisor.backtest import engine, synthetic_history

    universe = [i.symbol for i in AllocationPolicy().universe]
    history = synthetic_history(universe, start=date(2020, 1, 5), sessions=160)
    seen = []
    real = engine.decide

    def spy(universe_, closes, volumes, weights, params):
        seen.append(max((len(v) for v in closes.values()), default=0))
        return real(universe_, closes, volumes, weights, params)

    monkeypatch.setattr(engine, "decide", spy)
    engine.run_backtest(history, engine.BacktestConfig(four_factor=PARAMS))
    assert seen and seen[0] == 0, "the first decision sees no closes at all"
    assert all(b >= a for a, b in zip(seen, seen[1:], strict=False))
    assert max(seen) < len(history.days)


def test_the_lab_compares_it_with_the_plan_and_never_adopts_it(tmp_path):
    from egx_advisor.backtest import synthetic_history
    from egx_advisor.backtest.lab import adopt, run_strategy_comparison

    universe = [i.symbol for i in AllocationPolicy().universe]
    history = synthetic_history(universe, start=date(2020, 1, 5), sessions=400)
    report = run_strategy_comparison(history, synthetic=True)
    assert report.title and report.rules == () and not report.passed
    assert "four-factor" in report.render()
    with pytest.raises(ValueError):
        adopt(replace(report, synthetic=False), tmp_path / "rules.toml", date.today())


@pytest.mark.asyncio
async def test_the_live_agent_plans_no_trades_without_prices(tmp_path):
    agent, bus, computer, _news, _market, _agents = build(tmp_path, execute=False)
    agent.config = replace(agent.config, four_factor=PARAMS)
    _wire_tree(agent, computer)
    await agent._run_cycle()
    plan = bus.get("plan")["payload"]
    assert plan["four_factor"] and all(v["action"] == "no data"
                                       for v in plan["four_factor"].values())
    assert plan["orders"] == []


@pytest.mark.asyncio
async def test_the_live_agent_follows_the_four_factors(tmp_path):
    agent, bus, computer, _news, market, _agents = build(tmp_path, execute=False)
    agent.config = replace(agent.config, four_factor=PARAMS)
    today = SESSION_TIME.date()

    async def history(universe, *, days):
        rows = {}
        for n, instrument in enumerate(universe):
            closes = prices(step=0.002 if n % 2 == 0 else -0.002)
            rows[instrument.symbol] = [
                SimpleNamespace(day=today - timedelta(days=len(closes) - i), close=c, volume=v)
                for i, (c, v) in enumerate(zip(closes, volume(), strict=False))
            ]
        return rows

    market.history = history
    _wire_tree(agent, computer)
    await agent._run_cycle()
    plan = bus.get("plan")["payload"]
    actions = {s: v["action"] for s, v in plan["four_factor"].items()}
    assert "enter" in actions.values() or "hold" in actions.values()
    assert plan["policy_state"].startswith("FOUR-FACTOR:")
    for order in plan["orders"]:
        if order["side"] == "buy":
            assert actions[order["symbol"]] in ("enter", "hold")

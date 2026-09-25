"""The Strategy Lab and the rules it can adopt: filters that only ever skip buys."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from egx_advisor.backtest import BacktestConfig, run_backtest, synthetic_history
from egx_advisor.backtest.lab import adopt, remove, run_lab
from egx_advisor.strategy.filters import (
    BuyFilter,
    RuleError,
    apply_buy_filters,
    load_rules,
)
from egx_advisor.strategy.policy import AllocationPolicy
from egx_advisor.types import ProposedOrder, Side

SYMBOLS = [i.symbol for i in AllocationPolicy().universe]


def order(symbol="COMI.CA", side=Side.BUY):
    return ProposedOrder(symbol, side, D(10), D(80), "drift")


def rising(n=60):
    return [D(100 + i) for i in range(n)]


def falling(n=60):
    return [D(200 - i) for i in range(n)]


# --------------------------------------------------------------------- rules


def test_parameters_outside_their_bounds_are_refused() -> None:
    with pytest.raises(RuleError):
        BuyFilter("sma", {"days": 2})
    with pytest.raises(RuleError):
        BuyFilter("rsi", {"above": 99})
    with pytest.raises(RuleError):
        BuyFilter("macd_magic")


def test_sma_blocks_a_buy_below_the_average_only() -> None:
    rule = BuyFilter("sma", {"days": 20})
    assert rule.blocks(falling()) is not None
    assert rule.blocks(rising()) is None


def test_rsi_blocks_an_overbought_buy() -> None:
    rule = BuyFilter("rsi", {"days": 14, "above": 70})
    assert rule.blocks(rising()) is not None
    assert rule.blocks(falling()) is None


def test_momentum_blocks_after_a_sharp_fall() -> None:
    rule = BuyFilter("momentum", {"days": 20, "fall_pct": 10})
    crash = [D(100)] * 30 + [D(80)]
    assert rule.blocks(crash) is not None
    assert rule.blocks(rising()) is None


def test_too_little_history_skips_the_buy() -> None:
    assert "only 5 of" in BuyFilter("sma", {"days": 20}).blocks(rising(5))


def test_filters_never_touch_a_sell_and_never_add_an_order() -> None:
    rule = BuyFilter("sma", {"days": 20})
    orders = [order("COMI.CA", Side.BUY), order("COMI.CA", Side.SELL), order("ETEL.CA")]
    kept, dropped = apply_buy_filters(orders, [rule], {"COMI.CA": falling()})
    assert [o.side for o in kept] == [Side.SELL]
    assert {o.symbol for o, _ in dropped} == {"COMI.CA", "ETEL.CA"}
    assert all(o in orders for o in kept)


# ------------------------------------------------------------------- engine


def test_rules_in_the_backtest_see_no_future_prices() -> None:
    """Truncating the history must not change a day that remains."""
    history = synthetic_history(SYMBOLS, start=date(2022, 1, 2), sessions=200)
    config = BacktestConfig(buy_filters=(BuyFilter("sma", {"days": 20}),))
    full = run_backtest(history, config)
    short_days = tuple(history.days[:150])
    short = run_backtest(replace(history, days=short_days), config)
    assert [s.total_value for s in full.snapshots[:150]] == [s.total_value for s in short.snapshots]


# ---------------------------------------------------------------------- lab


def test_synthetic_prices_can_never_be_adopted(tmp_path: Path) -> None:
    history = synthetic_history(SYMBOLS, start=date(2021, 1, 3), sessions=300)
    report = run_lab(history, [BuyFilter("sma", {"days": 50})], synthetic=True)
    assert not report.passed
    assert "synthetic" in report.verdict()
    with pytest.raises(ValueError, match="not adopted"):
        adopt(report, tmp_path / "rules.toml", date(2026, 9, 25))
    assert not (tmp_path / "rules.toml").exists()


def test_the_lab_needs_enough_history() -> None:
    history = synthetic_history(SYMBOLS, start=date(2021, 1, 3), sessions=60)
    with pytest.raises(ValueError, match="sessions"):
        run_lab(history, [BuyFilter("sma", {"days": 20})])


def test_a_passing_report_is_adopted_and_can_be_removed(tmp_path: Path) -> None:
    history = synthetic_history(SYMBOLS, start=date(2021, 1, 3), sessions=300)
    rule = BuyFilter("rsi", {"days": 14, "above": 80})
    report = run_lab(history, [rule])
    # Force the verdict: this test is about adoption, not about the market.
    passing = replace(
        report,
        full=replace(report.full, difference=0.02, ci_low=0.01, ci_high=0.03),
        first_half=replace(report.first_half, difference=0.01),
        second_half=replace(report.second_half, difference=0.01),
    )
    path = tmp_path / "rules.toml"
    adopt(passing, path, date(2026, 9, 25))
    adopted = load_rules(path)
    assert [a.rule for a in adopted] == [rule]
    assert "sessions" in adopted[0].evidence
    remove(rule, path)
    assert load_rules(path) == ()


def test_a_rule_that_helps_in_only_one_half_fails() -> None:
    history = synthetic_history(SYMBOLS, start=date(2021, 1, 3), sessions=300)
    report = run_lab(history, [BuyFilter("sma", {"days": 50})])
    mixed = replace(
        report,
        full=replace(report.full, difference=0.02, ci_low=0.01, ci_high=0.03),
        first_half=replace(report.first_half, difference=0.05),
        second_half=replace(report.second_half, difference=-0.01),
    )
    assert not mixed.passed
    assert "one half" in mixed.verdict()


# ----------------------------------------------------------------- live agent


async def test_the_agent_skips_buys_an_adopted_rule_objects_to(tmp_path: Path) -> None:
    from egx_advisor.backtest.lab import adopt as _adopt  # noqa: F401 - import check
    from egx_advisor.strategy.filters import AdoptedRule, save_rules
    from tests.test_agent_pipeline import build

    agent, bus, *_ = build(tmp_path)
    agent.rules_path = tmp_path / "rules.toml"
    save_rules(agent.rules_path, [AdoptedRule(BuyFilter("sma", {"days": 20}),
                                              date(2026, 9, 1), "test")])

    class Row:
        def __init__(self, day, close):
            self.day, self.close = day, close

    async def history(universe, days):
        return {"COMI.CA": [Row(date(2026, 8, i + 1), D(200 - i)) for i in range(25)]}

    agent._market_data.history = history
    now = datetime(2026, 9, 24, 9, tzinfo=timezone.utc)
    kept, suppressed = await agent._apply_adopted_rules(
        now, [order("COMI.CA"), order("COMI.CA", Side.SELL)], []
    )
    assert [o.side for o in kept] == [Side.SELL]
    assert suppressed[0][1].startswith("lab rule:")


async def test_without_price_history_an_adopted_rule_skips_buys(tmp_path: Path) -> None:
    from egx_advisor.strategy.filters import AdoptedRule, save_rules
    from tests.test_agent_pipeline import build

    agent, *_ = build(tmp_path)
    agent.rules_path = tmp_path / "rules.toml"
    save_rules(agent.rules_path, [AdoptedRule(BuyFilter("sma", {"days": 20}),
                                              date(2026, 9, 1), "test")])
    kept, suppressed = await agent._apply_adopted_rules(
        datetime(2026, 9, 24, 9, tzinfo=timezone.utc), [order("COMI.CA")], []
    )
    assert kept == ()
    assert "only 0 of" in suppressed[0][1]

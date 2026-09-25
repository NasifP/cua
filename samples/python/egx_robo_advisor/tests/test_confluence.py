"""Trend + Momentum + Volume + Volatility: a buy goes ahead only when all four agree."""

from __future__ import annotations

from decimal import Decimal

import pytest

from egx_advisor.strategy.filters import RULE_KINDS, BuyFilter, apply_buy_filters
from egx_advisor.types import ProposedOrder, Side

RULE = BuyFilter("confluence", {})  # the defaults: 100/20/20/100%/20/3%
N = RULE.lookback + 5


def series(step: float = 0.002, wobble: float = 0.0, start: float = 100.0) -> list[Decimal]:
    prices, price = [], start
    for i in range(N):
        price *= 1 + step + (wobble if i % 2 else -wobble)
        prices.append(Decimal(f"{price:.4f}"))
    return prices


def volume(recent_share: float = 1.2) -> list[Decimal]:
    base = [Decimal(1_000_000)] * (N - 5)
    return base + [Decimal(int(1_000_000 * recent_share))] * 5


def test_all_four_agree_so_the_buy_goes_ahead():
    assert RULE.blocks(series(), volume()) is None


def test_a_falling_market_fails_trend_and_momentum():
    reason = RULE.blocks(series(step=-0.002), volume())
    assert "trend:" in reason and "momentum:" in reason
    assert "volume:" not in reason and "volatility:" not in reason


def test_drying_volume_fails_volume_only():
    reason = RULE.blocks(series(), volume(recent_share=0.5))
    assert reason.startswith("volume:") and "trend" not in reason


def test_a_violent_market_fails_volatility():
    reason = RULE.blocks(series(step=0.004, wobble=0.05), volume())
    assert "volatility:" in reason


def test_no_volume_data_is_a_reason_not_a_pass():
    assert "no volume data" in RULE.blocks(series(), [])
    assert "no volume data" in RULE.blocks(series(), [Decimal(0)] * N)


def test_too_little_history_skips_the_buy():
    assert "days of prices" in RULE.blocks(series()[:50], volume()[:50])


def test_parameters_are_bounded_like_every_rule():
    assert RULE_KINDS["confluence"]["params"]["max_vol_pct"] == (3, 1, 10)
    with pytest.raises(ValueError):
        BuyFilter("confluence", {"trend_days": 1000})
    tight = BuyFilter("confluence", {"volume_ratio": 300})
    assert "volume:" in tight.blocks(series(), volume())


def test_sells_always_pass_and_volumes_reach_the_rule():
    buy = ProposedOrder("COMI.CA", Side.BUY, Decimal(10), Decimal(80), "under target")
    sell = ProposedOrder("ETEL.CA", Side.SELL, Decimal(10), Decimal(30), "over target")
    kept, dropped = apply_buy_filters([buy, sell], [RULE], {"COMI.CA": series()},
                                      {"COMI.CA": volume(0.4)})
    assert kept == [sell] and dropped[0][1].startswith("volume:")
    kept, dropped = apply_buy_filters([buy], [RULE], {"COMI.CA": series()},
                                      {"COMI.CA": volume()})
    assert kept == [buy] and dropped == []


def test_description_in_both_languages():
    assert "100-day average" in RULE.describe()
    assert "100" in RULE.describe("ar") and "الأربعة" in RULE.describe("ar")


def test_the_lab_can_test_it():
    from datetime import date

    from egx_advisor.backtest import synthetic_history
    from egx_advisor.backtest.lab import run_lab
    from egx_advisor.strategy.policy import AllocationPolicy

    universe = [i.symbol for i in AllocationPolicy().universe]
    history = synthetic_history(universe, start=date(2020, 1, 5), sessions=500)
    report = run_lab(history, [RULE], synthetic=True)
    assert not report.passed  # synthetic prices never pass
    assert "all agree" in report.render()
    assert report.rules == (RULE,)

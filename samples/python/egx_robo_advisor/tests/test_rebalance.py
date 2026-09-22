"""Allocation policy and the drift-band rebalancer, including EGX frictions."""

from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from egx_advisor.strategy.policy import (
    AllocationPolicy,
    PolicyParameters,
    SleeveTargets,
)
from egx_advisor.strategy.rebalance import plan_rebalance
from egx_advisor.types import (
    MarketSnapshot,
    Portfolio,
    Position,
    Quote,
    Side,
    Sleeve,
    Tradability,
)

NOW = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)


def quote(symbol: str, last: str, prev: str | None = None, tradability=Tradability.OPEN) -> Quote:
    return Quote(symbol, D(last), D(prev or last), NOW, tradability)


def market(usd_egp: str = "50", lookback: str = "48", **overrides: Quote) -> MarketSnapshot:
    """Build a snapshot. Override keys use underscores (EAST_CA -> EAST.CA),
    since keyword arguments cannot contain a dot."""
    quotes = {
        "COMI.CA": quote("COMI.CA", "85"),
        "TMGH.CA": quote("TMGH.CA", "52"),
        "SWDY.CA": quote("SWDY.CA", "78"),
        "ABUK.CA": quote("ABUK.CA", "66"),
        "EAST.CA": quote("EAST.CA", "40"),
        "ETEL.CA": quote("ETEL.CA", "35"),
        "AZG.CA": quote("AZG.CA", "19"),
    }
    quotes.update({key.replace("_CA", ".CA"): value for key, value in overrides.items()})
    return MarketSnapshot(NOW, quotes, D(usd_egp), D(lookback))


def portfolio(cash: str = "1000000", unsettled: str = "0", **positions: str) -> Portfolio:
    return Portfolio(
        NOW,
        {sym: Position(sym, D(1), D(value)) for sym, value in positions.items()},
        cash_egp=D(cash),
        unsettled_cash_egp=D(unsettled),
    )


# ------------------------------------------------------------------- policy


def test_sleeve_targets_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match="sum to 1"):
        SleeveTargets(D("0.5"), D("0.3"), D("0.1"))


def test_fitted_parameters_are_rejected() -> None:
    """The anti-overfitting guardrail with teeth."""
    with pytest.raises(ValueError, match="not a sanctioned value"):
        PolicyParameters(sleeve_drift_band=D("0.0437"))
    with pytest.raises(ValueError, match="not a sanctioned value"):
        PolicyParameters(max_name_weight=D("0.1234"))


def test_sanctioned_parameters_are_accepted() -> None:
    assert PolicyParameters(sleeve_drift_band=D("0.08")).sleeve_drift_band == D("0.08")


def test_equity_names_are_equally_weighted() -> None:
    weights = AllocationPolicy().target_weights(D("0.05"))
    equity = [w for symbol, w in weights.items() if symbol != "AZG.CA"]
    assert len(set(equity)) == 1, "equal weighting is the whole allocation rule"


def test_devaluation_state_raises_the_hedge_and_cuts_equity() -> None:
    policy = AllocationPolicy()
    calm = policy.target_weights(D("0.05"))
    stressed = policy.target_weights(D("0.25"))
    assert stressed["AZG.CA"] > calm["AZG.CA"]
    assert stressed["COMI.CA"] < calm["COMI.CA"]


def test_name_cap_does_not_clamp_the_single_hedge_instrument() -> None:
    """A 15% single-issuer cap must not silently neuter a 45% hedge target.

    With one ETF in the sleeve, applying the issuer cap here would hold the
    hedge at 15% and make the entire devaluation response inert.
    """
    policy = AllocationPolicy()
    assert policy.target_weights(D("0.25"))["AZG.CA"] == D("0.45")


def test_equity_name_cap_still_applies() -> None:
    policy = AllocationPolicy(
        universe=(
            AllocationPolicy().by_sleeve(Sleeve.BLUE_CHIP)[0],
            AllocationPolicy().by_sleeve(Sleeve.INFLATION_HEDGE)[0],
        ),
        params=PolicyParameters(max_name_weight=D("0.10")),
    )
    # One blue chip would otherwise take the whole 60% sleeve.
    assert policy.target_weights(D("0.05"))["COMI.CA"] == D("0.10")


def test_universe_must_contain_a_hedge() -> None:
    with pytest.raises(ValueError, match="inflation hedge"):
        AllocationPolicy(universe=AllocationPolicy().by_sleeve(Sleeve.BLUE_CHIP))


# --------------------------------------------------------------- rebalancing


def test_no_orders_inside_the_drift_band() -> None:
    """A portfolio already near target must not churn."""
    policy = AllocationPolicy()
    targets = policy.target_weights(D("0.04"))
    total = D("1000000")
    holdings = {s: str(w * total) for s, w in targets.items()}
    book = portfolio(cash=str(total - sum(w * total for w in targets.values())), **holdings)
    plan = plan_rebalance(portfolio=book, market=market(), policy=policy)
    assert plan.is_empty


def test_underweight_generates_a_buy() -> None:
    plan = plan_rebalance(
        portfolio=portfolio(cash="1000000"), market=market(), policy=AllocationPolicy()
    )
    assert plan.orders
    assert all(o.side is Side.BUY for o in plan.orders)
    assert all(o.rationale for o in plan.orders)


def test_overweight_generates_a_sell() -> None:
    plan = plan_rebalance(
        portfolio=Portfolio(
            NOW,
            {"COMI.CA": Position("COMI.CA", D(1000), D("900000"))},
            cash_egp=D("100000"),
        ),
        market=market(),
        policy=AllocationPolicy(),
    )
    assert any(o.side is Side.SELL and o.symbol == "COMI.CA" for o in plan.orders)


def test_halted_names_are_skipped() -> None:
    plan = plan_rebalance(
        portfolio=portfolio(cash="1000000"),
        market=market(EAST_CA=quote("EAST.CA", "40", tradability=Tradability.HALTED)),
        policy=AllocationPolicy(),
    )
    assert any(s.symbol == "EAST.CA" and "halt" in s.reason for s in plan.skipped)
    assert not any(o.symbol == "EAST.CA" for o in plan.orders)


def test_limit_up_names_are_not_chased() -> None:
    """A rebalancer buying a limit-up name is momentum-chasing with extra steps."""
    plan = plan_rebalance(
        portfolio=portfolio(cash="1000000"),
        market=market(SWDY_CA=quote("SWDY.CA", "78", "71")),  # ~+9.9%
        policy=AllocationPolicy(),
    )
    reasons = {s.symbol: s.reason for s in plan.skipped}
    assert "SWDY.CA" in reasons and "not chasing" in reasons["SWDY.CA"]


def test_stale_quotes_are_skipped() -> None:
    plan = plan_rebalance(
        portfolio=portfolio(cash="1000000"),
        market=market(ETEL_CA=quote("ETEL.CA", "35", tradability=Tradability.STALE)),
        policy=AllocationPolicy(),
    )
    assert any(s.symbol == "ETEL.CA" and "stale" in s.reason for s in plan.skipped)


def test_unsettled_cash_is_excluded_from_buying_power() -> None:
    """EGX settles T+2; today's proceeds cannot fund today's buys."""
    plan = plan_rebalance(
        portfolio=portfolio(cash="1000000", unsettled="990000"),
        market=market(),
        policy=AllocationPolicy(),
    )
    assert any("unsettled" in note for note in plan.notes)
    assert plan.turnover_egp <= D("10000")


def test_orders_below_the_commission_floor_are_skipped() -> None:
    small = Portfolio(
        NOW,
        {"COMI.CA": Position("COMI.CA", D(1), D("3000"))},
        cash_egp=D("7000"),
    )
    plan = plan_rebalance(portfolio=small, market=market(), policy=AllocationPolicy())
    assert any("floor" in s.reason or "lot" in s.reason for s in plan.skipped)


def test_turnover_cap_trims_rather_than_drops_the_largest_correction() -> None:
    """The biggest drift is the most important thing to fix; never skip it.

    Dropping a budget-busting sell in favour of two small buys that happen to fit
    leaves the portfolio further from target than a partial fill would.
    """
    book = Portfolio(
        NOW,
        {"COMI.CA": Position("COMI.CA", D(3000), D("900000"))},
        cash_egp=D("100000"),
    )
    plan = plan_rebalance(portfolio=book, market=market(), policy=AllocationPolicy())
    sells = [o for o in plan.orders if o.side is Side.SELL]
    assert sells, "the dominant overweight must still be corrected"
    assert sells[0].symbol == "COMI.CA"
    assert "trimmed" in sells[0].rationale
    assert plan.turnover_egp <= D("0.15") * book.total_value


def test_sells_are_planned_before_buys() -> None:
    book = Portfolio(
        NOW,
        {"COMI.CA": Position("COMI.CA", D(3000), D("500000"))},
        cash_egp=D("500000"),
    )
    plan = plan_rebalance(portfolio=book, market=market(), policy=AllocationPolicy())
    sides = [o.side for o in plan.orders]
    if Side.SELL in sides and Side.BUY in sides:
        assert sides.index(Side.SELL) < sides.index(Side.BUY)


def test_cooldown_prevents_immediate_re_trading() -> None:
    plan = plan_rebalance(
        portfolio=portfolio(cash="1000000"),
        market=market(),
        policy=AllocationPolicy(),
        last_traded={"COMI.CA": date(2026, 9, 21)},
        today=date(2026, 9, 22),
    )
    assert any(s.symbol == "COMI.CA" and "cooldown" in s.reason for s in plan.skipped)


def test_lot_rounding_never_produces_a_fractional_order() -> None:
    from egx_advisor.types import Instrument

    policy = AllocationPolicy(
        universe=(
            Instrument("COMI.CA", "CIB", Sleeve.BLUE_CHIP, lot_size=100),
            Instrument("AZG.CA", "Gold", Sleeve.INFLATION_HEDGE),
        )
    )
    plan = plan_rebalance(
        portfolio=portfolio(cash="1000000"), market=market(), policy=policy
    )
    for order in plan.orders:
        if order.symbol == "COMI.CA":
            assert order.quantity % D(100) == 0


def test_empty_portfolio_produces_no_orders() -> None:
    empty = Portfolio(NOW, {}, cash_egp=D(0))
    plan = plan_rebalance(portfolio=empty, market=market(), policy=AllocationPolicy())
    assert plan.is_empty
    assert "no value" in plan.notes[0]


def test_every_order_carries_a_rationale() -> None:
    plan = plan_rebalance(
        portfolio=portfolio(cash="1000000"), market=market(), policy=AllocationPolicy()
    )
    assert plan.orders
    for order in plan.orders:
        assert order.rationale.strip()
        assert "target" in order.rationale


def test_plan_is_deterministic() -> None:
    """Same inputs, same plan -- the property that makes backtesting meaningful."""
    args = dict(portfolio=portfolio(cash="1000000"), market=market(), policy=AllocationPolicy())
    first = plan_rebalance(**args)
    second = plan_rebalance(**args)
    assert [(o.symbol, o.side, o.quantity) for o in first.orders] == [
        (o.symbol, o.side, o.quantity) for o in second.orders
    ]

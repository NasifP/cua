"""Backtester: no lookahead, realistic fills, and honest statistics."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from egx_advisor.backtest import (
    BacktestConfig,
    CostModel,
    FillModel,
    HistoryError,
    compare,
    compute,
    load_csv,
    run_backtest,
    run_buy_and_hold,
    sample_size_warning,
    synthetic_history,
)
from egx_advisor.backtest.metrics import TRADING_DAYS_PER_YEAR
from egx_advisor.backtest.types import Bar, BacktestResult, DaySnapshot, PriceHistory
from egx_advisor.strategy.policy import AllocationPolicy
from egx_advisor.types import ProposedOrder, Side, Tradability

POLICY = AllocationPolicy()
SYMBOLS = [i.symbol for i in POLICY.universe]
DAY = date(2026, 9, 22)


@pytest.fixture
def history() -> PriceHistory:
    return synthetic_history(SYMBOLS, start=date(2024, 1, 2), sessions=260, devaluation_at=130)


# ------------------------------------------------------------------ bar hygiene


def test_bar_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="low"):
        Bar(DAY, "COMI.CA", D(85), D(84), D(86), D(85))


def test_bar_rejects_non_positive_price() -> None:
    with pytest.raises(ValueError, match="non-positive"):
        Bar(DAY, "COMI.CA", D(85), D(86), D(0), D(85))


# ------------------------------------------------------------------ fill model


def bar(high="86", low="84", open_="85", close="85.5", volume="100000", trad=Tradability.OPEN) -> Bar:
    return Bar(DAY, "COMI.CA", D(open_), D(high), D(low), D(close), D(volume), trad)


def order(side=Side.BUY, qty="500", limit="85.4") -> ProposedOrder:
    return ProposedOrder("COMI.CA", side, D(qty), D(limit), "drift")


def test_buy_limit_below_the_days_low_does_not_fill() -> None:
    """The core anti-flattery rule: a limit only fills if the day traded there."""
    fill, rejection = FillModel().execute(order(limit="83"), bar(), DAY)
    assert fill is None
    assert "below the day's low" in rejection.reason


def test_sell_limit_above_the_days_high_does_not_fill() -> None:
    fill, rejection = FillModel().execute(order(Side.SELL, limit="90"), bar(), DAY)
    assert fill is None
    assert "above the day's high" in rejection.reason


def test_buy_never_fills_better_than_its_limit() -> None:
    fill, _ = FillModel().execute(order(), bar(), DAY)
    assert fill is not None
    assert fill.price <= D("85.4")


def test_sell_never_fills_worse_than_its_limit() -> None:
    fill, _ = FillModel().execute(order(Side.SELL, limit="85.0"), bar(), DAY)
    assert fill is not None
    assert fill.price >= D("85.0")


def test_volume_participation_caps_the_fill() -> None:
    """You cannot take 50% of a thin name's daily volume without moving it."""
    fill, rejection = FillModel().execute(order(qty="5000"), bar(volume="10000"), DAY)
    assert fill is not None
    assert fill.quantity == D(1000)  # 10% of 10,000
    assert "partial" in rejection.reason


@pytest.mark.parametrize(
    "trad,fragment",
    [(Tradability.HALTED, "halted"), (Tradability.STALE, "stale"),
     (Tradability.AT_PRICE_LIMIT, "price limit")],
)
def test_untradeable_bars_never_fill(trad, fragment) -> None:
    fill, rejection = FillModel().execute(order(), bar(trad=trad), DAY)
    assert fill is None and fragment in rejection.reason


def test_charges_are_applied_and_reduce_cash() -> None:
    fill, _ = FillModel().execute(order(), bar(), DAY)
    assert fill.charges > 0
    # A buy costs notional plus charges.
    assert fill.cash_delta == -(fill.notional + fill.charges).quantize(D("0.01"))


def test_frictionless_model_charges_nothing() -> None:
    fill, _ = FillModel(costs=CostModel.frictionless()).execute(order(), bar(), DAY)
    assert fill.charges == 0 and fill.slippage_cost == 0


# --------------------------------------------------------------------- engine


def test_engine_produces_a_snapshot_per_session(history: PriceHistory) -> None:
    result = run_backtest(history, BacktestConfig(policy=POLICY))
    assert len(result.snapshots) == len(history.days)
    assert all(s.total_value > 0 for s in result.snapshots)


def test_engine_is_deterministic(history: PriceHistory) -> None:
    a = run_backtest(history, BacktestConfig(policy=POLICY))
    b = run_backtest(history, BacktestConfig(policy=POLICY))
    assert [s.total_value for s in a.snapshots] == [s.total_value for s in b.snapshots]
    assert len(a.fills) == len(b.fills)


def test_costs_reduce_the_ending_value(history: PriceHistory) -> None:
    priced = run_backtest(history, BacktestConfig(policy=POLICY, costs=CostModel()))
    free = run_backtest(
        history, BacktestConfig(policy=POLICY, costs=CostModel.frictionless(), label="free")
    )
    assert priced.total_costs > 0
    assert free.total_costs == 0
    assert priced.ending_value < free.ending_value


def test_no_lookahead_future_bars_cannot_change_an_earlier_snapshot() -> None:
    """Truncating the history must not alter the days that remain.

    If a later bar could influence an earlier decision, the prefix run would
    differ from the full run over the same days. This is the single most
    important property a backtester can have.
    """
    full = synthetic_history(SYMBOLS, start=date(2024, 1, 2), sessions=200, devaluation_at=100)
    cut = 120
    prefix = PriceHistory(
        bars={d: full.bars[d] for d in full.days[:cut]},
        macro={d: full.macro[d] for d in full.days[:cut]},
        days=tuple(full.days[:cut]),
    )
    a = run_backtest(full, BacktestConfig(policy=POLICY))
    b = run_backtest(prefix, BacktestConfig(policy=POLICY))
    assert [s.total_value for s in a.snapshots[:cut]] == [s.total_value for s in b.snapshots]


def test_sale_proceeds_are_locked_for_t_plus_two(history: PriceHistory) -> None:
    result = run_backtest(history, BacktestConfig(policy=POLICY))
    # On at least one day some cash must be unsettled, otherwise T+2 is not modelled.
    assert any(s.settled_cash < s.cash for s in result.snapshots)


def test_rejections_are_recorded_not_silently_dropped(history: PriceHistory) -> None:
    """A plan is not a fill; hiding unfilled orders overstates tracking."""
    result = run_backtest(history, BacktestConfig(policy=POLICY))
    assert result.rejections, "a realistic fill model must reject some orders"


def test_buy_and_hold_drifts_away_from_target(history: PriceHistory) -> None:
    """Reporting zero drift for buy & hold would invert the whole comparison."""
    hold = run_buy_and_hold(history, BacktestConfig(policy=POLICY))
    rebalanced = run_backtest(history, BacktestConfig(policy=POLICY))
    hold_drift = compute(hold).mean_drift
    policy_drift = compute(rebalanced).mean_drift
    assert hold_drift > 0
    assert hold_drift > policy_drift, "rebalancing must track the policy more closely"


def test_empty_history_is_rejected() -> None:
    empty = PriceHistory(bars={}, macro={}, days=())
    with pytest.raises(ValueError, match="no session days"):
        run_backtest(empty, BacktestConfig(policy=POLICY))


# -------------------------------------------------------------------- metrics


def synth_result(label: str, daily: float, days: int = 400) -> BacktestResult:
    result = BacktestResult(label=label, starting_value=D(1000000))
    value = 1_000_000.0
    for i in range(days):
        value *= 1 + daily
        result.snapshots.append(
            DaySnapshot(date(2024, 1, 1) + timedelta(days=i), D(str(round(value, 2))),
                        D(0), D(0), {}, D(0), D(0), D(0))
        )
    return result


def test_identical_series_compare_as_noise() -> None:
    a, b = synth_result("a", 0.0004), synth_result("b", 0.0004)
    comparison = compare(a, b, iterations=400)
    assert not comparison.conclusive
    assert "INDISTINGUISHABLE" in comparison.render()


def test_a_large_consistent_edge_is_detected() -> None:
    """The interval must not be so wide that it can never conclude anything."""
    a, b = synth_result("a", 0.0010), synth_result("b", 0.0001)
    comparison = compare(a, b, iterations=400)
    assert comparison.conclusive
    assert comparison.difference > 0


def test_short_samples_never_claim_a_conclusion() -> None:
    a, b = synth_result("a", 0.002, days=20), synth_result("b", 0.0001, days=20)
    assert not compare(a, b, iterations=200).conclusive


def test_sample_size_warning_scales_with_history() -> None:
    assert "Far too short" in sample_size_warning(300)
    assert "not enough to choose parameters" in sample_size_warning(
        int(TRADING_DAYS_PER_YEAR * 5)
    )
    assert sample_size_warning(int(TRADING_DAYS_PER_YEAR * 10)) is None


def test_metrics_report_costs_and_fill_rate(history: PriceHistory) -> None:
    metrics = compute(run_backtest(history, BacktestConfig(policy=POLICY)))
    assert metrics.total_costs > 0
    assert 0 < metrics.fill_rate <= 1
    assert "costs paid" in metrics.render()


# ----------------------------------------------------------------------- csv


def test_load_csv_round_trip(tmp_path) -> None:
    path = tmp_path / "px.csv"
    path.write_text(
        "date,symbol,open,high,low,close,volume\n"
        "2026-09-20,COMI.CA,85,86,84,85.5,100000\n"
        "2026-09-21,COMI.CA,85.5,87,85,86.5,120000\n",
        encoding="utf-8",
    )
    history = load_csv(path)
    assert history.symbols() == {"COMI.CA"}
    assert history.bars_on(date(2026, 9, 21))["COMI.CA"].close == D("86.5")


def test_load_csv_rejects_a_bad_bar_rather_than_repairing_it(tmp_path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(
        "date,symbol,open,high,low,close\n2026-09-20,COMI.CA,85,84,86,85\n", encoding="utf-8"
    )
    with pytest.raises(HistoryError):
        load_csv(path)


def test_load_csv_rejects_missing_columns(tmp_path) -> None:
    path = tmp_path / "thin.csv"
    path.write_text("date,symbol,close\n2026-09-20,COMI.CA,85\n", encoding="utf-8")
    with pytest.raises(HistoryError, match="missing columns"):
        load_csv(path)

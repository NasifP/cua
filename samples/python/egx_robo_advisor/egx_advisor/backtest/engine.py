"""Replay engine: walk the history one session at a time.

The engine's only real responsibility is to avoid lying to the strategy. It
therefore:

  * hands `plan_rebalance` **only** the bars for the day being simulated, so
    there is no path by which tomorrow's price informs today's order;
  * prices the plan off the *previous* close and fills it against the *current*
    day's range, which is the order of events a real session has;
  * carries T+2 settlement, so proceeds of a sale are not spendable for two
    session days, exactly as the live agent experiences;
  * records rejections as well as fills, because a plan is not a fill and a
    backtest that silently drops unfilled orders overstates how well the policy
    tracked its targets.

The regime filter can be replayed too, by supplying dated headlines. It is
optional: with no news the run measures the strategy and its frictions alone,
which is usually what you want to look at first.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from ..clock import TradingCalendar
from ..regime.filter import RegimeFilter
from ..regime.sentiment import KeywordClassifier
from ..strategy.policy import AllocationPolicy
from ..strategy.rebalance import plan_rebalance
from ..types import (
    Headline,
    MarketSnapshot,
    Portfolio,
    Position,
    Quote,
    RiskState,
    Side,
    Tradability,
)
from .fills import FillModel
from .types import Bar, BacktestResult, CostModel, DaySnapshot, PriceHistory


@dataclass
class BacktestConfig:
    policy: AllocationPolicy = field(default_factory=AllocationPolicy)
    costs: CostModel = field(default_factory=CostModel)
    fills: Optional[FillModel] = None
    starting_cash: Decimal = Decimal("1000000")
    starting_positions: Mapping[str, Decimal] = field(default_factory=dict)
    calendar: TradingCalendar = field(default_factory=TradingCalendar)
    #: Replay the circuit breaker using dated headlines.
    headlines: Mapping[date, Sequence[Headline]] = field(default_factory=dict)
    #: Rebalance at most this often. None means every session the bands allow.
    min_days_between_cycles: int = 1
    label: str = "policy"

    def fill_model(self) -> FillModel:
        return self.fills or FillModel(costs=self.costs)


def run_backtest(history: PriceHistory, config: BacktestConfig) -> BacktestResult:
    """Replay `history` under `config`. Deterministic: same inputs, same result."""
    result = BacktestResult(label=config.label)
    fill_model = config.fill_model()
    policy = config.policy

    quantities: dict[str, Decimal] = {
        symbol: Decimal(q) for symbol, q in config.starting_positions.items()
    }
    cash = Decimal(config.starting_cash)
    # (settlement day, amount) for sale proceeds not yet spendable.
    pending: deque[tuple[date, Decimal]] = deque()
    last_traded: dict[str, date] = {}
    last_cycle: Optional[date] = None

    regime_filter = RegimeFilter() if config.headlines else None
    classifier = KeywordClassifier() if config.headlines else None

    days = [d for d in history.days if config.calendar.is_session_day(d)]
    if not days:
        raise ValueError("history contains no session days")

    previous_close: dict[str, Decimal] = {}
    result.starting_value = _value(quantities, history.bars_on(days[0]), cash)

    for day in days:
        bars = history.bars_on(day)
        if not bars:
            continue

        # Release anything that has settled by today.
        while pending and pending[0][0] <= day:
            pending.popleft()
        unsettled = sum((amount for _, amount in pending), Decimal(0))

        costs_today = Decimal(0)
        turnover_today = Decimal(0)
        risk_state = RiskState.RISK_ON

        due = last_cycle is None or (day - last_cycle).days >= config.min_days_between_cycles
        if due:
            market = _market_snapshot(day, bars, previous_close, history.usd_egp_on(day), policy)
            portfolio = Portfolio(
                as_of=_as_datetime(day),
                positions={
                    symbol: Position(symbol, qty, qty * _close(bars, symbol, previous_close))
                    for symbol, qty in quantities.items()
                    if qty > 0
                },
                cash_egp=cash,
                unsettled_cash_egp=min(unsettled, cash),
                demo_confirmed=True,
            )

            plan = plan_rebalance(
                portfolio=portfolio,
                market=market,
                policy=policy,
                last_traded=last_traded,
                today=day,
            )
            orders = plan.orders

            if regime_filter is not None and classifier is not None:
                assessments = classifier.classify(config.headlines.get(day, ()))
                regime = regime_filter.evaluate(
                    assessments, now=_as_datetime(day), feed_age_seconds=60.0
                )
                risk_state = regime.risk_state
                orders, _ = regime_filter.apply(orders, regime)

            for order in orders:
                instrument = policy.instrument(order.symbol)
                lot = instrument.lot_size if instrument else 1
                fill, rejection = fill_model.execute(order, bars.get(order.symbol), day, lot)
                if rejection is not None:
                    result.rejections.append(rejection)
                if fill is None:
                    continue

                result.fills.append(fill)
                cash += fill.cash_delta
                costs_today += fill.charges + fill.slippage_cost
                turnover_today += fill.notional
                last_traded[order.symbol] = day

                if fill.side is Side.BUY:
                    quantities[fill.symbol] = quantities.get(fill.symbol, Decimal(0)) + fill.quantity
                else:
                    quantities[fill.symbol] = quantities.get(fill.symbol, Decimal(0)) - fill.quantity
                    # Proceeds are not spendable until T+2.
                    pending.append(
                        (config.calendar.settlement_date(day), fill.notional - fill.charges)
                    )

            if orders:
                last_cycle = day

        for symbol, bar in bars.items():
            previous_close[symbol] = bar.close

        total = _value(quantities, bars, cash, previous_close)
        weights = {
            symbol: (qty * _close(bars, symbol, previous_close) / total) if total > 0 else Decimal(0)
            for symbol, qty in quantities.items()
            if qty > 0
        }
        targets = policy.target_weights(_depreciation(history, day, config))
        drift = sum(
            (abs(targets.get(s, Decimal(0)) - weights.get(s, Decimal(0))) for s in targets),
            Decimal(0),
        )

        result.snapshots.append(
            DaySnapshot(
                day=day,
                total_value=total,
                cash=cash,
                settled_cash=max(Decimal(0), cash - sum((a for _, a in pending), Decimal(0))),
                weights=weights,
                total_drift=drift,
                costs_paid=costs_today,
                turnover=turnover_today,
                risk_state=risk_state.value,
            )
        )

    return result


def run_buy_and_hold(history: PriceHistory, config: BacktestConfig) -> BacktestResult:
    """Baseline: buy the target weights once on day one, then never trade again.

    This is the comparison that matters for a rebalancer. If rebalancing does not
    beat leaving the portfolio alone once its costs are paid, the bands or the
    turnover cap are wrong -- and that is a conclusion about frictions, which is
    what this backtester is for.
    """
    days = [d for d in history.days if config.calendar.is_session_day(d)]
    if not days:
        raise ValueError("history contains no session days")

    first = days[0]
    bars = history.bars_on(first)
    targets = config.policy.target_weights(_depreciation(history, first, config))
    fill_model = config.fill_model()

    quantities: dict[str, Decimal] = {}
    cash = Decimal(config.starting_cash)
    result = BacktestResult(label=f"{config.label} (buy & hold)")
    result.starting_value = cash

    for symbol, weight in targets.items():
        bar = bars.get(symbol)
        if bar is None:
            continue
        instrument = config.policy.instrument(symbol)
        lot = instrument.lot_size if instrument else 1
        budget = cash * weight
        quantity = (budget / bar.close).to_integral_value() // lot * lot
        if quantity <= 0:
            continue
        notional = quantity * bar.close
        charges = fill_model.costs.charges(notional)
        cash -= notional + charges
        quantities[symbol] = Decimal(quantity)

    previous_close: dict[str, Decimal] = {}
    for day in days:
        day_bars = history.bars_on(day)
        if not day_bars:
            continue
        for symbol, bar in day_bars.items():
            previous_close[symbol] = bar.close
        total = _value(quantities, day_bars, cash, previous_close)
        weights = {
            s: (q * _close(day_bars, s, previous_close) / total) if total > 0 else Decimal(0)
            for s, q in quantities.items()
        }
        # Drift is measured the same way as the policy run. Reporting zero here
        # would be badly misleading: drifting away from target is exactly what
        # buy & hold does, and it is the cost this comparison exists to weigh
        # against the commission the policy pays to avoid it.
        day_targets = config.policy.target_weights(_depreciation(history, day, config))
        drift = sum(
            (
                abs(day_targets.get(s, Decimal(0)) - weights.get(s, Decimal(0)))
                for s in day_targets
            ),
            Decimal(0),
        )
        result.snapshots.append(
            DaySnapshot(
                day=day,
                total_value=total,
                cash=cash,
                settled_cash=cash,
                weights=weights,
                total_drift=drift,
                costs_paid=Decimal(0),
                turnover=Decimal(0),
            )
        )
    return result


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _market_snapshot(
    day: date,
    bars: Mapping[str, Bar],
    previous_close: Mapping[str, Decimal],
    usd_egp: Optional[Decimal],
    policy: AllocationPolicy,
) -> MarketSnapshot:
    """Build the snapshot the strategy sees. Prices off the PREVIOUS close.

    Using today's close to size an order we then fill during today would be
    lookahead: at the moment the live bot plans, today's close does not exist.
    """
    quotes: dict[str, Quote] = {}
    for instrument in policy.universe:
        bar = bars.get(instrument.symbol)
        if bar is None:
            continue
        prior = previous_close.get(instrument.symbol, bar.open)
        last = prior
        tradability = bar.tradability
        if tradability is Tradability.OPEN and prior > 0:
            move = (bar.open - prior) / prior
            if abs(move) >= instrument.price_limit_pct:
                tradability = Tradability.AT_PRICE_LIMIT
        quotes[instrument.symbol] = Quote(
            symbol=instrument.symbol,
            last=last,
            prev_close=prior,
            as_of=_as_datetime(day),
            tradability=tradability,
        )
    reference = usd_egp or Decimal("50")
    return MarketSnapshot(
        as_of=_as_datetime(day),
        quotes=quotes,
        usd_egp=reference,
        usd_egp_lookback=reference,
    )


def _depreciation(history: PriceHistory, day: date, config: BacktestConfig) -> Decimal:
    """Trailing EGP depreciation over the policy's lookback window."""
    current = history.usd_egp_on(day)
    if current is None:
        return Decimal(0)
    lookback_days = config.policy.params.devaluation_lookback_days
    earlier = [d for d in history.macro if (day - d).days >= lookback_days]
    if not earlier:
        return Decimal(0)
    reference = history.macro[max(earlier)].usd_egp
    if reference <= 0:
        return Decimal(0)
    return (current - reference) / reference


def _close(bars: Mapping[str, Bar], symbol: str, fallback: Mapping[str, Decimal]) -> Decimal:
    bar = bars.get(symbol)
    if bar is not None:
        return bar.close
    return fallback.get(symbol, Decimal(0))


def _value(
    quantities: Mapping[str, Decimal],
    bars: Mapping[str, Bar],
    cash: Decimal,
    fallback: Optional[Mapping[str, Decimal]] = None,
) -> Decimal:
    """Mark the book, carrying forward the last known close for a missing bar.

    Skipping a symbol that has no bar today values the holding at zero, which
    reads as a total loss on that name for that session and then a recovery the
    next day -- a drawdown that never happened. Synthetic histories never show
    this because every symbol has every day; real feeds have gaps per symbol, so
    this is the first thing real data would have broken.
    """
    fallback = fallback or {}
    total = cash
    for symbol, qty in quantities.items():
        if qty == 0:
            continue
        bar = bars.get(symbol)
        price = bar.close if bar is not None else fallback.get(symbol)
        if price is None:
            # Never held a valued bar: there is nothing honest to mark it at.
            continue
        total += qty * price
    return total


def _as_datetime(day: date) -> datetime:
    return datetime.combine(day, time(12, 0), tzinfo=timezone.utc)

"""Value types for the backtester.

What this backtester is for
---------------------------
Measuring **frictions**, not discovering parameters. It answers questions the
live bot cannot: what do commissions and slippage actually cost over a year, how
far from the policy weights does the portfolio really sit, does rebalancing at
this band beat not rebalancing once costs are paid.

It is explicitly NOT for choosing `sleeve_drift_band` by trying all three and
keeping the best-performing one. That is curve fitting, the sample is far too
small to support it, and `PolicyParameters.validate()` plus
docs/NO_OVERFIT_CHARTER.md exist to prevent it. `metrics.py` reports a bootstrap
confidence interval on every comparison precisely so that a difference which is
indistinguishable from noise is visibly labelled as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from ..types import Side, Tradability


@dataclass(frozen=True, slots=True)
class Bar:
    """One session's prices for one symbol.

    `high`/`low` drive the fill model: a limit order only fills if the day
    actually traded through it. Using the close alone would silently assume every
    limit fills, which flatters the results in exactly the way that matters.
    """

    day: date
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(0)
    tradability: Tradability = Tradability.OPEN

    def __post_init__(self) -> None:
        if self.low > self.high:
            raise ValueError(f"{self.symbol} {self.day}: low {self.low} > high {self.high}")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError(f"{self.symbol} {self.day}: non-positive price")


@dataclass(frozen=True, slots=True)
class MacroBar:
    """The one macro series the policy reads."""

    day: date
    usd_egp: Decimal


@dataclass
class PriceHistory:
    """Bars indexed by day, plus the EGP series.

    Holds no forward-looking data by construction: `bars_on(day)` returns only
    that day's bars, and the engine never hands the strategy anything later.
    """

    bars: Mapping[date, Mapping[str, Bar]]
    macro: Mapping[date, MacroBar]
    #: Session days in order. The engine walks exactly these.
    days: Sequence[date] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.days:
            self.days = tuple(sorted(self.bars))

    def bars_on(self, day: date) -> Mapping[str, Bar]:
        return self.bars.get(day, {})

    def usd_egp_on(self, day: date) -> Optional[Decimal]:
        entry = self.macro.get(day)
        return entry.usd_egp if entry else None

    def symbols(self) -> frozenset[str]:
        return frozenset(s for row in self.bars.values() for s in row)


@dataclass(frozen=True, slots=True)
class CostModel:
    """Transaction costs. Every field is a fact to confirm with your broker.

    The defaults are placeholders sized to be *pessimistic rather than
    flattering*, because a backtest that understates costs is worse than no
    backtest: it makes over-trading look free. Replace them with the real
    schedule before drawing any conclusion about turnover.
    """

    #: Broker commission, as a fraction of notional.
    commission_rate: Decimal = Decimal("0.0020")
    #: Minimum commission per order, in EGP.
    commission_min: Decimal = Decimal("10")
    #: Exchange / regulator fees as a fraction of notional.
    exchange_fee_rate: Decimal = Decimal("0.0005")
    #: Transaction stamp duty as a fraction of notional. CONFIRM THE CURRENT
    #: RATE AND WHICH SIDE PAYS IT -- Egyptian stamp duty has changed repeatedly.
    stamp_duty_rate: Decimal = Decimal("0.0005")
    #: Slippage actually suffered versus the limit, as a fraction. Thin EGX names
    #: rarely fill at the touch.
    slippage_rate: Decimal = Decimal("0.0010")

    def charges(self, notional: Decimal) -> Decimal:
        """Explicit costs on one fill (commission, fees, duty). Excludes slippage."""
        commission = max(notional * self.commission_rate, self.commission_min)
        return commission + notional * (self.exchange_fee_rate + self.stamp_duty_rate)

    @classmethod
    def frictionless(cls) -> "CostModel":
        """Zero costs. Used only as a comparison baseline, never as a scenario."""
        return cls(
            commission_rate=Decimal(0),
            commission_min=Decimal(0),
            exchange_fee_rate=Decimal(0),
            stamp_duty_rate=Decimal(0),
            slippage_rate=Decimal(0),
        )


@dataclass(frozen=True, slots=True)
class Fill:
    day: date
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    charges: Decimal
    #: Cash effect including charges: negative for a buy, positive for a sell.
    cash_delta: Decimal
    #: Difference between the limit and the achieved price, in EGP.
    slippage_cost: Decimal

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True, slots=True)
class Rejection:
    """An order the market did not take. Kept: a plan is not a fill."""

    day: date
    symbol: str
    side: Side
    quantity: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class DaySnapshot:
    day: date
    total_value: Decimal
    cash: Decimal
    settled_cash: Decimal
    weights: Mapping[str, Decimal]
    #: Sum of |actual - target| across the universe: how far from policy we sit.
    total_drift: Decimal
    costs_paid: Decimal
    turnover: Decimal
    risk_state: str = "risk_on"


@dataclass
class BacktestResult:
    label: str
    snapshots: list[DaySnapshot] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    starting_value: Decimal = Decimal(0)

    @property
    def ending_value(self) -> Decimal:
        return self.snapshots[-1].total_value if self.snapshots else self.starting_value

    @property
    def total_costs(self) -> Decimal:
        return sum((f.charges + f.slippage_cost for f in self.fills), Decimal(0))

    @property
    def total_turnover(self) -> Decimal:
        return sum((f.notional for f in self.fills), Decimal(0))

    def daily_returns(self) -> list[float]:
        out: list[float] = []
        for prev, cur in zip(self.snapshots, self.snapshots[1:], strict=False):
            if prev.total_value > 0:
                out.append(float((cur.total_value - prev.total_value) / prev.total_value))
        return out

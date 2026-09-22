"""Core value types shared by every layer of the EGX robo-advisor.

Everything here is frozen and I/O-free on purpose. The strategy and regime
layers consume only these types, which is what makes them deterministic,
backtestable, and unit-testable without a screen, a broker, or a network.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping, Optional, Sequence

# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #


class Sleeve(enum.Enum):
    """Structural buckets of the portfolio.

    Deliberately coarse. Three sleeves is the whole policy surface; see
    docs/NO_OVERFIT_CHARTER.md for why we refuse to add a fourth without an
    out-of-sample reason that survives an EGP devaluation.
    """

    BLUE_CHIP = "blue_chip"
    INFLATION_HEDGE = "inflation_hedge"
    CASH = "cash"


@dataclass(frozen=True, slots=True)
class Instrument:
    """An EGX-listed instrument the bot is allowed to hold."""

    symbol: str
    name: str
    sleeve: Sleeve
    #: EGX trades in board lots; orders are floored to a multiple of this.
    lot_size: int = 1
    #: Daily price-limit band as a fraction (EGX default is 10% either way;
    #: a handful of names and newly listed issues run wider).
    price_limit_pct: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        if not self.symbol.endswith(".CA") and self.sleeve is not Sleeve.CASH:
            raise ValueError(f"expected an EGX (.CA) symbol, got {self.symbol!r}")
        if self.lot_size < 1:
            raise ValueError("lot_size must be >= 1")


# --------------------------------------------------------------------------- #
# Market and portfolio state
# --------------------------------------------------------------------------- #


class Tradability(enum.Enum):
    """Whether the exchange will actually accept an order in this name."""

    OPEN = "open"
    #: Printed at its upper or lower daily limit. We never chase these.
    AT_PRICE_LIMIT = "at_price_limit"
    #: Exchange-level suspension or a pending disclosure halt.
    HALTED = "halted"
    #: No print recent enough to size an order against.
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    last: Decimal
    #: Previous official close, used to detect limit-band proximity.
    prev_close: Decimal
    as_of: datetime
    tradability: Tradability = Tradability.OPEN

    @property
    def move_from_prev_close(self) -> Decimal:
        if self.prev_close == 0:
            return Decimal(0)
        return (self.last - self.prev_close) / self.prev_close


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Prices at a single instant, plus the EGP reference the hedge reacts to."""

    as_of: datetime
    quotes: Mapping[str, Quote]
    #: USD/EGP official rate. The inflation-hedge floor keys off its trailing
    #: depreciation, which is the one macro input the policy is allowed to see.
    usd_egp: Decimal
    #: USD/EGP as of `devaluation_lookback_days` ago.
    usd_egp_lookback: Decimal

    def quote(self, symbol: str) -> Optional[Quote]:
        return self.quotes.get(symbol)

    @property
    def egp_depreciation(self) -> Decimal:
        """Trailing EGP depreciation as a positive fraction (0.35 == -35% EGP)."""
        if self.usd_egp_lookback == 0:
            return Decimal(0)
        return (self.usd_egp - self.usd_egp_lookback) / self.usd_egp_lookback


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: Decimal
    #: Marked value in EGP.
    market_value: Decimal


@dataclass(frozen=True, slots=True)
class Portfolio:
    """A point-in-time read of the (simulated) Thndr account."""

    as_of: datetime
    positions: Mapping[str, Position]
    cash_egp: Decimal
    #: Cash still locked by EGX T+2 settlement and therefore unspendable today.
    unsettled_cash_egp: Decimal = Decimal(0)
    #: True only when the read came from a screen confirmed to be the simulator.
    demo_confirmed: bool = False

    @property
    def total_value(self) -> Decimal:
        return sum((p.market_value for p in self.positions.values()), Decimal(0)) + self.cash_egp

    @property
    def investable_cash(self) -> Decimal:
        return max(Decimal(0), self.cash_egp - self.unsettled_cash_egp)

    def weight_of(self, symbol: str) -> Decimal:
        total = self.total_value
        if total <= 0:
            return Decimal(0)
        pos = self.positions.get(symbol)
        return (pos.market_value / total) if pos else Decimal(0)


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #


class Side(enum.Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class ProposedOrder:
    """A rebalancing intent, not yet an instruction to the screen.

    `rationale` is mandatory. Every order rendered on the dashboard has to be
    explainable in one sentence, otherwise it is a black box we cannot audit
    after a bad session.
    """

    symbol: str
    side: Side
    quantity: Decimal
    limit_price: Decimal
    rationale: str
    #: Drift that triggered this order, in absolute weight terms.
    drift: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if not self.rationale.strip():
            raise ValueError("every order needs a rationale")

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.limit_price

    def scaled_to(self, quantity: Decimal, reason: str) -> "ProposedOrder":
        """Return a strictly smaller copy. Used by the regime filter only."""
        if quantity > self.quantity:
            raise ValueError("scaled_to may only shrink an order")
        return replace(
            self, quantity=quantity, rationale=f"{self.rationale} | trimmed: {reason}"
        )


# --------------------------------------------------------------------------- #
# Regime
# --------------------------------------------------------------------------- #


class RiskState(enum.Enum):
    """The only three states the news layer is allowed to express.

    Ordered by restrictiveness. The regime filter can move us down this list
    but the strategy layer can never move us up it -- see regime/filter.py.
    """

    RISK_ON = "risk_on"
    #: De-risking (sells) still permitted; no new exposure.
    BUYS_HALTED = "buys_halted"
    #: Nothing touches the screen.
    ALL_HALTED = "all_halted"

    @property
    def rank(self) -> int:
        return {"risk_on": 0, "buys_halted": 1, "all_halted": 2}[self.value]


class Severity(enum.Enum):
    """News severity taxonomy. Note there is no positive band, by design."""

    NONE = "none"
    ELEVATED = "elevated"
    CATASTROPHIC = "catastrophic"


class Scope(enum.Enum):
    MARKET = "market"
    SECTOR = "sector"
    TICKER = "ticker"


@dataclass(frozen=True, slots=True)
class Headline:
    source: str
    title: str
    url: str
    published_at: datetime
    summary: str = ""

    @property
    def dedupe_key(self) -> str:
        return self.url or f"{self.source}:{self.title}"


@dataclass(frozen=True, slots=True)
class NewsAssessment:
    """The classifier's verdict on one headline. Never a tradeable score."""

    headline: Headline
    severity: Severity
    scope: Scope
    categories: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RegimeState:
    """What the strategy and execution layers are told about the world's mood."""

    as_of: datetime
    risk_state: RiskState
    #: Symbols on which buying is forbidden regardless of the market-wide state.
    blocked_symbols: frozenset[str] = frozenset()
    #: Human-readable drivers, newest first, for the dashboard.
    drivers: tuple[str, ...] = ()
    #: Age of the freshest successfully polled feed.
    feed_age_seconds: Optional[float] = None
    stale: bool = False

    @classmethod
    def unknown(cls, now: Optional[datetime] = None) -> "RegimeState":
        """The state we start in and fall back to: no news means no buying."""
        return cls(
            as_of=now or datetime.now(timezone.utc),
            risk_state=RiskState.BUYS_HALTED,
            drivers=("no news assessment yet; buys withheld until feeds report",),
            stale=True,
        )


# --------------------------------------------------------------------------- #
# Agent lifecycle
# --------------------------------------------------------------------------- #


class AgentPhase(enum.Enum):
    IDLE = "idle"
    POLLING_NEWS = "polling_news"
    READING_PORTFOLIO = "reading_portfolio"
    PLANNING = "planning"
    ASSERTING_DEMO = "asserting_demo"
    EXECUTING = "executing"
    HALTED = "halted"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CycleReport:
    """One full pass of the agent loop, as surfaced on the dashboard."""

    started_at: datetime
    phase: AgentPhase
    regime: RegimeState
    proposed: Sequence[ProposedOrder] = field(default_factory=tuple)
    approved: Sequence[ProposedOrder] = field(default_factory=tuple)
    executed: Sequence[ProposedOrder] = field(default_factory=tuple)
    notes: tuple[str, ...] = ()

"""The market-data seam: protocol, errors, and the rule that bad data blocks.

Why this is a seam at all
-------------------------
Prices are the denominator of every weight and the multiplier on every order
size. An OCR error, a stale tape, an unadjusted split, or a figure quoted in the
wrong currency does not produce a slightly worse trade -- it produces an order
off by a factor. So the strategy layer never reads prices off the screen, and a
provider is required to *validate* what it fetched rather than pass it through.

The rule that follows: a provider that cannot vouch for its data raises rather
than returning a degraded snapshot. Same posture as the demo guard -- every
"we cannot tell" resolves towards not trading, because the asymmetry demands it.
A missed rebalance costs a few basis points of tracking error; an order sized off
bad data costs real money.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, Sequence

from ..types import Instrument, MarketSnapshot


class MarketDataError(RuntimeError):
    """No usable snapshot. Callers must not fall back to stale prices."""


class DataQualityError(MarketDataError):
    """The data arrived but failed validation. Carries the findings."""

    def __init__(self, message: str, report: "QualityReport") -> None:
        super().__init__(message)
        self.report = report


class MarketDataProvider(Protocol):
    """Supplies a priced snapshot for the universe."""

    async def snapshot(self, universe: Sequence[Instrument]) -> MarketSnapshot: ...


class Severity(enum.Enum):
    """`BLOCKING` stops the snapshot. `WARN` is surfaced but allowed through."""

    WARN = "warn"
    BLOCKING = "blocking"


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    symbol: str
    severity: Severity
    detail: str

    def render(self) -> str:
        mark = "BLOCK" if self.severity is Severity.BLOCKING else " warn"
        return f"  [{mark}] {self.symbol:10} {self.code:22} {self.detail}"


@dataclass
class QualityReport:
    """What validation found. Empty is the only clean result."""

    findings: list[Finding] = field(default_factory=list)
    symbols_checked: int = 0

    def add(self, code: str, symbol: str, severity: Severity, detail: str) -> None:
        """Record a finding, keeping only the first of each (code, symbol).

        The bar validator and the quote validator overlap deliberately -- one
        checks the series, the other the derived snapshot -- so the same fault
        surfaces twice. Counting it twice makes "5 blocking findings" read as
        five distinct problems when it is three, which is exactly the kind of
        small dishonesty that erodes trust in the report.
        """
        if any(f.code == code and f.symbol == symbol for f in self.findings):
            return
        self.findings.append(Finding(code, symbol, severity, detail))

    def extend(self, findings: Sequence[Finding]) -> None:
        """Merge another validator's findings, deduplicating as `add` does."""
        for finding in findings:
            self.add(finding.code, finding.symbol, finding.severity, finding.detail)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKING]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARN]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def render(self) -> str:
        if not self.findings:
            return f"  no findings across {self.symbols_checked} symbol(s)"
        return "\n".join(f.render() for f in self.findings)

    def raise_if_blocking(self) -> None:
        if self.blocking:
            summary = "; ".join(f"{f.symbol}: {f.code}" for f in self.blocking[:5])
            raise DataQualityError(
                f"{len(self.blocking)} blocking data-quality finding(s): {summary}", self
            )


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    """Validation thresholds. Tuned to catch data faults, not market moves."""

    #: The currency every EGX quote must be in. A provider silently returning USD
    #: would scale every order by roughly 50x.
    required_currency: str = "EGP"
    #: A bar older than this many session days cannot size an order.
    max_bar_age_sessions: int = 3
    #: Day-over-day moves beyond this are implausible on EGX given its ±10%
    #: limit bands, and usually mean an unadjusted corporate action rather than a
    #: real move. Set above the band so a genuine limit day is not flagged.
    implausible_move: Decimal = Decimal("0.25")
    #: Yahoo's EGX bars sometimes print an open or close a little outside that
    #: day's high/low. Within this much of the range it is a warning, and the
    #: bar is used as is; further out it is a bad bar and blocks, as before.
    range_tolerance: Decimal = Decimal("0.03")
    #: Below this, treat a session as effectively untradeable for our sizes.
    min_daily_volume: Decimal = Decimal("1000")
    #: Warn when a symbol's history has gaps bigger than this many session days.
    max_history_gap_sessions: int = 5

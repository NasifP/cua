"""Validation for fetched prices.

Every check here exists because the corresponding fault produces a *wrongly
sized order*, not a slightly worse one:

  ``CURRENCY_MISMATCH``  a USD figure treated as EGP scales orders ~50x.
  ``IMPLAUSIBLE_MOVE``   EGX runs ±10% limit bands, so a 60% day-over-day move
                         is almost always an unadjusted split or a bad print.
                         Sizing against the pre-split price buys 5x the shares.
  ``STALE_BAR``          yesterday's close during a devaluation is not a price.
  ``MISSING_SYMBOL``     a universe member with no data silently drops out of the
                         denominator, inflating every other weight.
  ``NON_POSITIVE``       a zero price makes quantity = notional / 0.
  ``INVERTED_RANGE``     low above high means the row is not what it claims.

Severity is chosen by what the fault can do, not how unusual it looks. Anything
that can misprice an order is BLOCKING; anything that merely reduces confidence
is a warning. `ZERO_VOLUME` is deliberately only a warning: it may be a halt, a
holiday the calendar does not know, or thin trading, and we cannot tell which
from a price feed alone.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from ..types import Instrument, Quote
from .base import QualityPolicy, QualityReport, Severity


def validate_quotes(
    quotes: Mapping[str, Quote],
    universe: Sequence[Instrument],
    *,
    policy: Optional[QualityPolicy] = None,
    currencies: Optional[Mapping[str, str]] = None,
    volumes: Optional[Mapping[str, Decimal]] = None,
    now: Optional[datetime] = None,
) -> QualityReport:
    """Check a live snapshot before it is allowed to size anything."""
    policy = policy or QualityPolicy()
    now = now or datetime.now(timezone.utc)
    report = QualityReport(symbols_checked=len(universe))

    for instrument in universe:
        symbol = instrument.symbol
        quote = quotes.get(symbol)

        if quote is None:
            # Blocking: a missing member is not a zero weight, it is an unknown
            # one, and treating it as absent inflates every other weight.
            report.add("MISSING_SYMBOL", symbol, Severity.BLOCKING, "no quote returned")
            continue

        if currencies is not None:
            currency = (currencies.get(symbol) or "").upper()
            if currency and currency != policy.required_currency:
                report.add(
                    "CURRENCY_MISMATCH", symbol, Severity.BLOCKING,
                    f"quoted in {currency}, expected {policy.required_currency}",
                )

        if quote.last <= 0 or quote.prev_close <= 0:
            report.add(
                "NON_POSITIVE", symbol, Severity.BLOCKING,
                f"last={quote.last} prev_close={quote.prev_close}",
            )
            continue

        move = quote.move_from_prev_close
        if abs(move) >= policy.implausible_move:
            report.add(
                "IMPLAUSIBLE_MOVE", symbol, Severity.BLOCKING,
                f"{move:+.1%} vs prev close: beyond the ±{instrument.price_limit_pct:.0%} "
                f"band, likely an unadjusted corporate action",
            )

        age = now - quote.as_of
        if age > timedelta(days=policy.max_bar_age_sessions * 2):
            report.add(
                "STALE_BAR", symbol, Severity.BLOCKING,
                f"as of {quote.as_of.date()}, {age.days}d old",
            )

        if volumes is not None:
            volume = volumes.get(symbol, Decimal(0))
            if volume <= 0:
                report.add(
                    "ZERO_VOLUME", symbol, Severity.WARN,
                    "no volume: could be a halt, a holiday, or a data gap",
                )
            elif volume < policy.min_daily_volume:
                report.add(
                    "THIN_VOLUME", symbol, Severity.WARN,
                    f"{volume:.0f} shares: orders may not fill",
                )

    return report


def validate_bars(
    rows: Sequence["BarLike"],
    symbol: str,
    *,
    policy: Optional[QualityPolicy] = None,
    session_days: Optional[Sequence[date]] = None,
) -> QualityReport:
    """Check a historical series before it is used for a backtest.

    A backtest on bad history is worse than no backtest: it produces a number
    that looks like evidence.
    """
    policy = policy or QualityPolicy()
    report = QualityReport(symbols_checked=1)

    if not rows:
        report.add("EMPTY_HISTORY", symbol, Severity.BLOCKING, "no rows returned")
        return report

    seen: set[date] = set()
    previous_close: Optional[Decimal] = None
    previous_day: Optional[date] = None

    for row in sorted(rows, key=lambda r: r.day):
        if row.day in seen:
            report.add("DUPLICATE_DATE", symbol, Severity.BLOCKING, f"{row.day} appears twice")
            continue
        seen.add(row.day)

        if min(row.open, row.high, row.low, row.close) <= 0:
            report.add("NON_POSITIVE", symbol, Severity.BLOCKING, f"{row.day}: non-positive price")
            continue
        if row.low > row.high:
            report.add(
                "INVERTED_RANGE", symbol, Severity.BLOCKING,
                f"{row.day}: low {row.low} above high {row.high}",
            )
            continue
        if not (row.low <= row.close <= row.high and row.low <= row.open <= row.high):
            above = max(row.open, row.close) - row.high
            below = row.low - min(row.open, row.close)
            excursion = max(above / row.high, below / row.low)
            small = excursion <= policy.range_tolerance
            report.add(
                "CLOSE_OUTSIDE_RANGE", symbol, Severity.WARN if small else Severity.BLOCKING,
                f"{row.day}: open/close outside [{row.low}, {row.high}] by {excursion:.1%}"
                + (" (within tolerance: used as is)" if small else ""),
            )

        if previous_close is not None and previous_close > 0:
            move = (row.close - previous_close) / previous_close
            if abs(move) >= policy.implausible_move:
                report.add(
                    "IMPLAUSIBLE_MOVE", symbol, Severity.BLOCKING,
                    f"{row.day}: {move:+.1%} vs previous close "
                    f"({previous_close} -> {row.close})",
                )

        if previous_day is not None and session_days is not None:
            missing = [
                d for d in session_days if previous_day < d < row.day
            ]
            if len(missing) > policy.max_history_gap_sessions:
                report.add(
                    "HISTORY_GAP", symbol, Severity.WARN,
                    f"{len(missing)} session day(s) missing between "
                    f"{previous_day} and {row.day}",
                )

        previous_close = row.close
        previous_day = row.day

    return report


class BarLike:
    """Structural stand-in: anything with day/open/high/low/close Decimals."""

    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

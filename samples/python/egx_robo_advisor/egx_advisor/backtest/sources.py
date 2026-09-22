"""Bridge: a live market-data provider becomes backtest history.

Running the backtest on a different data source than the bot trades on measures
something other than the bot. So the same `YahooMarketData` that prices live
orders also supplies the history here, and the same `quality.py` checks run over
it -- a backtest on bad data is worse than no backtest, because it produces a
number that looks like evidence.

Two things this adds beyond a type conversion:

  * **A coverage report.** Real feeds cover symbols unevenly. A name present for
    300 of 1,200 sessions is not a holding the backtest can say anything about,
    and silently including it produces a confident-looking result about a
    portfolio that never existed.
  * **A CSV cache.** Five years across seven names is a slow and impolite fetch
    to repeat on every run. The cache is plain CSV so it is inspectable and can
    be diffed against the exchange by hand.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ..clock import TradingCalendar
from ..marketdata.base import QualityReport
from ..marketdata.yahoo import USD_EGP_SYMBOL, YahooRow
from ..types import Instrument, Tradability
from .types import Bar, MacroBar, PriceHistory

logger = logging.getLogger(__name__)


@dataclass
class CoverageReport:
    """Per-symbol coverage against the exchange calendar."""

    rows: dict[str, tuple[int, int, Optional[date], Optional[date]]] = field(
        default_factory=dict
    )
    #: Symbols whose coverage is too sparse to backtest honestly.
    unusable: list[str] = field(default_factory=list)

    def add(
        self, symbol: str, observed: int, expected: int, first: Optional[date], last: Optional[date]
    ) -> None:
        self.rows[symbol] = (observed, expected, first, last)

    def coverage(self, symbol: str) -> float:
        observed, expected, _, _ = self.rows.get(symbol, (0, 0, None, None))
        return (observed / expected) if expected else 0.0

    def render(self) -> str:
        lines = []
        for symbol, (observed, expected, first, last) in sorted(self.rows.items()):
            ratio = (observed / expected) if expected else 0.0
            flag = "  <-- SPARSE" if symbol in self.unusable else ""
            span = f"{first} .. {last}" if first else "no rows"
            lines.append(
                f"  {symbol:10} {observed:>5}/{expected:<5} sessions  {ratio:>5.0%}  {span}{flag}"
            )
        return "\n".join(lines)


def build_history(
    rows: Mapping[str, Sequence[YahooRow]],
    universe: Sequence[Instrument],
    *,
    calendar: Optional[TradingCalendar] = None,
    min_coverage: float = 0.80,
    macro_symbol: str = USD_EGP_SYMBOL,
) -> tuple[PriceHistory, CoverageReport]:
    """Convert provider rows into a `PriceHistory`, reporting coverage.

    Symbols below `min_coverage` are reported as unusable but still included:
    dropping a universe member changes the policy's target weights, which is a
    decision for the caller to make deliberately rather than a side effect of a
    patchy download.
    """
    calendar = calendar or TradingCalendar()
    coverage = CoverageReport()

    symbols = [i.symbol for i in universe]
    present = {s: sorted(rows.get(s) or (), key=lambda r: r.day) for s in symbols}
    dated = [r.day for series in present.values() for r in series]
    if not dated:
        raise ValueError("provider returned no rows for any universe symbol")

    first_day, last_day = min(dated), max(dated)
    session_days = _session_days(calendar, first_day, last_day)
    expected = len(session_days)

    bars: dict[date, dict[str, Bar]] = {day: {} for day in session_days}
    for symbol, series in present.items():
        kept = 0
        for row in series:
            if row.day not in bars:
                # A bar on a day the calendar calls closed. Usually a holiday the
                # holiday set does not know about; dropped rather than traded on.
                continue
            bars[row.day][symbol] = Bar(
                day=row.day,
                symbol=symbol,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                # Halts are never inferred from price data -- see marketdata/yahoo.py.
                tradability=Tradability.OPEN,
            )
            kept += 1
        coverage.add(
            symbol,
            kept,
            expected,
            series[0].day if series else None,
            series[-1].day if series else None,
        )
        if expected and (kept / expected) < min_coverage:
            coverage.unusable.append(symbol)

    macro: dict[date, MacroBar] = {}
    for row in sorted(rows.get(macro_symbol) or (), key=lambda r: r.day):
        macro[row.day] = MacroBar(day=row.day, usd_egp=row.close)

    if not macro:
        logger.warning(
            "no %s series: the devaluation switch cannot fire in this backtest, so the "
            "policy stays at baseline throughout", macro_symbol,
        )

    populated = tuple(d for d in session_days if bars[d])
    history = PriceHistory(
        bars={d: bars[d] for d in populated}, macro=macro, days=populated
    )
    return history, coverage


def _session_days(calendar: TradingCalendar, first: date, last: date) -> list[date]:
    from datetime import timedelta

    days: list[date] = []
    cursor = first
    while cursor <= last:
        if calendar.is_session_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


# --------------------------------------------------------------------------- #
# CSV cache
# --------------------------------------------------------------------------- #

CACHE_COLUMNS = ["date", "symbol", "open", "high", "low", "close", "volume", "currency"]


def write_cache(path: str | Path, rows: Mapping[str, Sequence[YahooRow]]) -> int:
    """Persist fetched rows as plain CSV, so they can be inspected and diffed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CACHE_COLUMNS)
        for symbol in sorted(rows):
            for row in sorted(rows[symbol], key=lambda r: r.day):
                writer.writerow([
                    row.day.isoformat(), symbol, row.open, row.high,
                    row.low, row.close, row.volume, row.currency,
                ])
                written += 1
    return written


def read_cache(path: str | Path) -> dict[str, list[YahooRow]]:
    """Load a cache written by `write_cache`."""
    out: dict[str, list[YahooRow]] = {}
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for line, record in enumerate(csv.DictReader(handle), start=2):
            try:
                out.setdefault(record["symbol"].strip().upper(), []).append(
                    YahooRow(
                        day=date.fromisoformat(record["date"]),
                        open=Decimal(record["open"]),
                        high=Decimal(record["high"]),
                        low=Decimal(record["low"]),
                        close=Decimal(record["close"]),
                        volume=Decimal(record.get("volume") or 0),
                        currency=(record.get("currency") or "EGP").strip(),
                    )
                )
            except (KeyError, ValueError, ArithmeticError) as exc:
                raise ValueError(f"{path}:{line}: {exc}") from exc
    return out


async def fetch_history(
    provider,
    universe: Sequence[Instrument],
    *,
    days: int = 1825,
    cache: Optional[str | Path] = None,
    refresh: bool = False,
    calendar: Optional[TradingCalendar] = None,
    min_coverage: float = 0.80,
) -> tuple[PriceHistory, CoverageReport, Optional[QualityReport]]:
    """Fetch (or load) history and convert it for the backtester."""
    rows: Mapping[str, Sequence[YahooRow]]
    quality: Optional[QualityReport] = None

    if cache and Path(cache).exists() and not refresh:
        logger.info("using cached history at %s", cache)
        rows = read_cache(cache)
    else:
        rows = await provider.history(universe, days=days)
        quality = getattr(provider, "last_report", None)
        if cache:
            written = write_cache(cache, rows)
            logger.info("cached %d rows to %s", written, cache)

    history, coverage = build_history(
        rows, universe, calendar=calendar, min_coverage=min_coverage
    )
    return history, coverage, quality

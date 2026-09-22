"""Yahoo Finance provider for EGX.

Why Yahoo at all
----------------
Yahoo quotes EGX names with the same ``.CA`` suffix this codebase already uses
(``COMI.CA``), so it is the shortest path from "no prices" to "a working
pipeline", and `yfinance` needs no key. That is its only advantage, and it is
worth being blunt about the rest:

  * **It is delayed and unofficial.** Treat it as good enough to develop and
    backtest against, and verify it against the exchange before you let it size
    a live order. `verify_market_data.py` exists for exactly that check.
  * **It cannot tell you about halts.** A trading suspension looks like a
    missing or zero-volume bar, which is indistinguishable from a holiday the
    calendar does not know about. So `Tradability.HALTED` is never inferred here
    -- the live agent must learn about halts from EGX disclosures, which is what
    the regime layer's authoritative feed is for. Claiming otherwise would be
    worse than admitting the gap.
  * **Corporate actions are the main failure mode.** An unadjusted split shows
    up as a large overnight move, which `quality.py` blocks on rather than
    sizing an order against.

The network call is injected (`fetch`), so the provider is fully testable
offline and a different backend can be dropped in without touching the mapping
or validation logic.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Optional, Sequence

from ..types import Instrument, MarketSnapshot, Quote, Tradability
from .base import (
    DataQualityError,
    MarketDataError,
    QualityPolicy,
    QualityReport,
    Severity,
)
from .quality import validate_bars, validate_quotes

logger = logging.getLogger(__name__)

#: Yahoo's symbol for the USD/EGP rate. Verify before relying on it: the
#: devaluation switch keys off this series, so a wrong symbol silently disables
#: the entire macro response.
USD_EGP_SYMBOL = "EGP=X"


@dataclass(frozen=True, slots=True)
class YahooRow:
    """One OHLCV row as returned by the fetch function."""

    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(0)
    currency: str = "EGP"


#: fetch(symbols, lookback_days) -> {symbol: [rows oldest-first]}
FetchFn = Callable[[Sequence[str], int], Mapping[str, Sequence[YahooRow]]]


@dataclass
class YahooMarketData:
    """Implements `MarketDataProvider` over yfinance.

    Refuses to return a snapshot that fails validation. A provider that degrades
    quietly is how an order gets sized against a pre-split price.
    """

    quality: QualityPolicy = field(default_factory=QualityPolicy)
    #: Calendar days of history pulled to establish prev_close and run checks.
    lookback_days: int = 30
    #: Injected for testing; resolved to yfinance on first use when None.
    fetch: Optional[FetchFn] = None
    #: Include the USD/EGP series so the devaluation switch has an input.
    include_macro: bool = True
    #: Calendar days back for the depreciation reference point.
    macro_lookback_days: int = 180
    #: Set False only for exploration. Live sizing must never see unvalidated data.
    enforce_quality: bool = True

    _last_report: Optional[QualityReport] = None

    # ------------------------------------------------------------------ fetching

    def _resolve_fetch(self) -> FetchFn:
        if self.fetch is not None:
            return self.fetch
        self.fetch = _yfinance_fetch
        return self.fetch

    async def snapshot(self, universe: Sequence[Instrument]) -> MarketSnapshot:
        symbols = [i.symbol for i in universe]
        wanted = list(symbols)
        if self.include_macro:
            wanted.append(USD_EGP_SYMBOL)

        fetch = self._resolve_fetch()
        try:
            # yfinance is synchronous and does blocking I/O; keep it off the loop
            # so a slow fetch cannot stall the agent's kill-switch checks.
            rows = await asyncio.to_thread(fetch, wanted, self.lookback_days)
        except Exception as exc:  # noqa: BLE001
            raise MarketDataError(f"yahoo fetch failed: {exc}") from exc

        if not rows:
            raise MarketDataError("yahoo returned no data at all")

        quotes: dict[str, Quote] = {}
        currencies: dict[str, str] = {}
        volumes: dict[str, Decimal] = {}
        report = QualityReport(symbols_checked=len(universe))

        for instrument in universe:
            series = list(rows.get(instrument.symbol) or ())
            if not series:
                report.add(
                    "MISSING_SYMBOL", instrument.symbol, Severity.BLOCKING,
                    "yahoo returned no rows; check the ticker resolves",
                )
                continue

            series.sort(key=lambda r: r.day)
            bar_report = validate_bars(series, instrument.symbol, policy=self.quality)
            report.extend(bar_report.findings)

            latest = series[-1]
            prior = series[-2] if len(series) > 1 else latest
            currencies[instrument.symbol] = latest.currency
            volumes[instrument.symbol] = latest.volume

            quotes[instrument.symbol] = Quote(
                symbol=instrument.symbol,
                last=latest.close,
                prev_close=prior.close,
                as_of=_as_datetime(latest.day),
                # Never HALTED: Yahoo cannot distinguish a suspension from a
                # holiday or a data gap. Halts come from EGX disclosures.
                tradability=(
                    Tradability.STALE
                    if _is_stale(latest.day, self.quality.max_bar_age_sessions)
                    else Tradability.OPEN
                ),
            )

        quote_report = validate_quotes(
            quotes, universe, policy=self.quality, currencies=currencies, volumes=volumes
        )
        report.extend(quote_report.findings)
        self._last_report = report

        if report.warnings:
            logger.info(
                "market data warnings:\n%s",
                "\n".join(f.render() for f in report.warnings),
            )
        if self.enforce_quality:
            report.raise_if_blocking()
        elif report.blocking:
            logger.error(
                "market data BLOCKING findings ignored (enforce_quality=False):\n%s",
                "\n".join(f.render() for f in report.blocking),
            )

        usd_egp, usd_egp_lookback = self._macro(rows)

        return MarketSnapshot(
            as_of=datetime.now(timezone.utc),
            quotes=quotes,
            usd_egp=usd_egp,
            usd_egp_lookback=usd_egp_lookback,
        )

    def _macro(self, rows: Mapping[str, Sequence[YahooRow]]) -> tuple[Decimal, Decimal]:
        """Current and lagged USD/EGP.

        When the series is unavailable, both come back equal, which yields zero
        depreciation and leaves the policy in its baseline state. That is the
        conservative direction: a missing FX series must not *trigger* a
        devaluation response, only fail to detect one -- and the absence is
        reported as a warning rather than silently assumed to be calm.
        """
        series = list(rows.get(USD_EGP_SYMBOL) or ())
        if not series:
            if self.include_macro and self._last_report is not None:
                self._last_report.add(
                    "MISSING_FX", USD_EGP_SYMBOL, Severity.WARN,
                    "no USD/EGP series: the devaluation switch cannot fire",
                )
            return Decimal("1"), Decimal("1")

        series.sort(key=lambda r: r.day)
        current = series[-1].close
        cutoff = series[-1].day
        older = [r for r in series if (cutoff - r.day).days >= self.macro_lookback_days]
        reference = older[-1].close if older else series[0].close
        return current, (reference if reference > 0 else current)

    @property
    def last_report(self) -> Optional[QualityReport]:
        """Findings from the most recent snapshot, for the dashboard and the CLI."""
        return self._last_report

    # ---------------------------------------------------------------- backtesting

    async def history(
        self, universe: Sequence[Instrument], *, days: int = 1825
    ) -> Mapping[str, Sequence[YahooRow]]:
        """Pull a long series for the backtester, validated per symbol.

        Same provider feeding both the live path and the backtest is the point:
        a backtest run on a different data source than the bot trades on is
        measuring something other than the bot.
        """
        fetch = self._resolve_fetch()
        symbols = [i.symbol for i in universe]
        if self.include_macro:
            symbols.append(USD_EGP_SYMBOL)
        try:
            rows = await asyncio.to_thread(fetch, symbols, days)
        except Exception as exc:  # noqa: BLE001
            raise MarketDataError(f"yahoo history fetch failed: {exc}") from exc

        report = QualityReport(symbols_checked=len(universe))
        for instrument in universe:
            series = rows.get(instrument.symbol) or ()
            report.extend(
                validate_bars(series, instrument.symbol, policy=self.quality).findings
            )
        self._last_report = report
        if self.enforce_quality:
            report.raise_if_blocking()
        return rows


# --------------------------------------------------------------------------- #
# yfinance backend
# --------------------------------------------------------------------------- #


def _yfinance_fetch(symbols: Sequence[str], lookback_days: int) -> dict[str, list[YahooRow]]:
    """Default backend. Imported lazily so the package stays dependency-free."""
    try:
        import yfinance
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MarketDataError(
            "yfinance is not installed. Install the marketdata extra: "
            "pip install -e '.[marketdata]'"
        ) from exc

    period = f"{max(lookback_days, 5)}d"
    frame = yfinance.download(
        list(symbols),
        period=period,
        interval="1d",
        auto_adjust=False,  # we want raw prices; adjustment hides split faults
        progress=False,
        group_by="ticker",
        threads=True,
    )
    if frame is None or frame.empty:
        raise MarketDataError(f"yfinance returned nothing for {list(symbols)}")

    out: dict[str, list[YahooRow]] = {}
    for symbol in symbols:
        try:
            sub = frame[symbol] if len(symbols) > 1 else frame
        except KeyError:
            continue
        rows: list[YahooRow] = []
        for index, row in sub.dropna(how="all").iterrows():
            try:
                rows.append(
                    YahooRow(
                        day=index.date() if hasattr(index, "date") else index,
                        open=_dec(row["Open"]),
                        high=_dec(row["High"]),
                        low=_dec(row["Low"]),
                        close=_dec(row["Close"]),
                        volume=_dec(row.get("Volume", 0)),
                        currency="EGP",  # verified separately; see verify_market_data.py
                    )
                )
            except (KeyError, TypeError, InvalidOperation):
                continue
        if rows:
            out[symbol] = rows
    return out


def _dec(value: object) -> Decimal:
    return Decimal(str(round(float(value), 6)))  # type: ignore[arg-type]


def _as_datetime(day: date) -> datetime:
    return datetime.combine(day, time(14, 30), tzinfo=timezone.utc)


def _is_stale(day: date, max_age_sessions: int) -> bool:
    return (date.today() - day).days > max_age_sessions * 2

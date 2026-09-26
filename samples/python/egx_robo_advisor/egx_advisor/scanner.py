"""Scan EGX stocks for the best four-factor set-ups, for the chat to explain.

"Find me the best 5 opportunities" is answered by code, not by a model. For
each stock in the scan list (config/scan_universe.toml) this computes, from
daily prices up to the previous session:

- trend: close against its 100-day average;
- momentum: 20- and 60-day return;
- volume: 5-day average volume against the 20-day average;
- volatility: standard deviation of daily returns over 20 days;
- RSI(14), for context only.

Stocks are ranked first by how many of the four factors agree (the same test
the four-factor strategy uses to enter), then by 60-day return per unit of
volatility. The chat model receives the ranked table and explains it; it is
told not to add, drop or reorder names, so it cannot invent a pick or a
number. None of this places or prepares an order.

A stock Yahoo cannot price, or whose prices fail the data checks, is left
out and listed as skipped.
"""

from __future__ import annotations

import asyncio
import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .strategy.four_factor import FourFactorParams
from .types import Instrument, Sleeve

MAX_TOP = 10
HISTORY_DAYS = 200  # calendar days: enough sessions for a 100-day average and more


@dataclass(frozen=True)
class Opportunity:
    symbol: str
    close: float
    trend_pct: float        # close vs the trend average, %
    momentum_20: float      # %
    momentum_60: float      # %
    volume_ratio: float     # 5-day vs volume-days average, %
    volatility: float       # daily %, std
    rsi: float
    agree: dict[str, bool]

    @property
    def agreeing(self) -> int:
        return sum(self.agree.values())

    @property
    def quality(self) -> float:
        """60-day return per unit of daily volatility: the tie-breaker."""
        return self.momentum_60 / max(self.volatility, 0.1)


@dataclass(frozen=True)
class ScanResult:
    top: tuple[Opportunity, ...]
    scanned: int
    skipped: tuple[str, ...]
    as_of: str
    params: FourFactorParams = field(default_factory=FourFactorParams)

    def table(self) -> str:
        """The ranked rows as plain text: what the chat model is given."""
        lines = [f"scan of {self.scanned} EGX stocks, prices up to the previous session "
                 f"(as of {self.as_of}); ranked by factors agreeing, then 60-day return "
                 f"per unit of volatility"]
        for rank, o in enumerate(self.top, 1):
            flags = ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in o.agree.items())
            lines.append(
                f"{rank}. {o.symbol}: close {o.close:.2f} EGP; {o.agreeing}/4 agree ({flags}); "
                f"vs {self.params.trend_days}-day average {o.trend_pct:+.1f}%; "
                f"20-day {o.momentum_20:+.1f}%, 60-day {o.momentum_60:+.1f}%; "
                f"volume {o.volume_ratio:.0f}% of average; volatility {o.volatility:.1f}%/day; "
                f"RSI {o.rsi:.0f}"
            )
        if self.skipped:
            lines.append("skipped (no usable prices): " + ", ".join(self.skipped))
        return "\n".join(lines)


def _rsi(prices: Sequence[float], days: int = 14) -> float:
    window = prices[-(days + 1):]
    gains = sum(max(b - a, 0) for a, b in zip(window, window[1:], strict=False))
    losses = sum(max(a - b, 0) for a, b in zip(window, window[1:], strict=False))
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100 - 100 / (1 + gains / losses)


def analyze(symbol: str, closes: Sequence[Decimal], volumes: Sequence[Decimal],
            params: FourFactorParams = FourFactorParams()) -> Optional[Opportunity]:  # noqa: B008
    """The four factors for one stock, or None without enough history."""
    need = max(params.trend_days, 61, params.volume_days, params.vol_days + 1) + 1
    if len(closes) < need:
        return None
    prices = [float(c) for c in closes]
    last = prices[-1]
    average = sum(prices[-params.trend_days:]) / params.trend_days
    momentum_20 = (last / prices[-21] - 1) * 100
    momentum_60 = (last / prices[-61] - 1) * 100
    momentum_n = (last / prices[-(params.momentum_days + 1)] - 1) * 100
    vols = [float(v) for v in volumes[-params.volume_days:]]
    volume_ratio = ((sum(vols[-5:]) / 5) / (sum(vols) / len(vols)) * 100
                    if len(vols) == params.volume_days and sum(vols) > 0 else 0.0)
    window = prices[-(params.vol_days + 1):]
    returns = [(b / a - 1) * 100 for a, b in zip(window, window[1:], strict=False) if a]
    mean = sum(returns) / len(returns)
    volatility = (sum((r - mean) ** 2 for r in returns) / len(returns)) ** 0.5
    return Opportunity(
        symbol=symbol, close=last, trend_pct=(last / average - 1) * 100,
        momentum_20=momentum_20, momentum_60=momentum_60, volume_ratio=volume_ratio,
        volatility=volatility, rsi=_rsi(prices),
        agree={
            "trend": last > average,
            "momentum": momentum_n > 0,
            "volume": volume_ratio >= params.volume_ratio,
            "volatility": 0 < volatility <= params.max_vol_pct,
        },
    )


def rank(opportunities: Sequence[Opportunity], top: int) -> tuple[Opportunity, ...]:
    ordered = sorted(opportunities, key=lambda o: (o.agreeing, o.quality), reverse=True)
    return tuple(ordered[:max(1, min(top, MAX_TOP))])


def load_universe(path: Path) -> list[str]:
    """Symbols from config/scan_universe.toml, de-duplicated, in file order."""
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    symbols = [str(s).strip().upper() for s in raw.get("symbols", []) if str(s).strip()]
    return list(dict.fromkeys(s if s.endswith(".CA") else s + ".CA" for s in symbols))


async def scan(market_data: Any, symbols: Sequence[str], top: int = 5,
               params: FourFactorParams = FourFactorParams()) -> ScanResult:  # noqa: B008
    """Fetch history for `symbols`, analyse each, and rank."""
    universe = [Instrument(s, s, Sleeve.BLUE_CHIP) for s in symbols]
    rows: Mapping[str, Sequence[Any]] = await market_data.history(universe, days=HISTORY_DAYS)
    report = getattr(market_data, "last_report", None)
    blocked = {f.symbol for f in getattr(report, "blocking", ())} if report else set()
    found, skipped = [], []
    for symbol in symbols:
        series = sorted(rows.get(symbol) or (), key=lambda r: r.day)
        today = datetime.now(timezone.utc).date()
        series = [r for r in series if r.day < today]  # the previous session at the latest
        opportunity = None if symbol in blocked else analyze(
            symbol, [r.close for r in series], [getattr(r, "volume", 0) for r in series], params)
        (found.append(opportunity) if opportunity else skipped.append(symbol))
    return ScanResult(top=rank(found, top), scanned=len(symbols), skipped=tuple(skipped),
                      as_of=datetime.now(timezone.utc).date().isoformat(), params=params)


def yahoo_scan(top: int = 5, universe_path: Optional[Path] = None) -> ScanResult:
    """The default scanner: Yahoo prices, the configured scan list."""
    from .marketdata import YahooMarketData
    from .paths import config_path

    symbols = load_universe(universe_path or config_path("scan_universe.toml"))
    if not symbols:
        from .strategy.policy import AllocationPolicy

        symbols = [i.symbol for i in AllocationPolicy().universe]
    # Checks still run and a bad stock is skipped, but one bad stock must not
    # stop the scan of the others.
    source = YahooMarketData(include_macro=False, enforce_quality=False)
    return asyncio.run(scan(source, symbols, top))


# --------------------------------------------------------------------------- #
# Recognising a scan request in the chat
# --------------------------------------------------------------------------- #

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
#: A scan request asks to look (find, best, top, scan...) for something to
#: trade (opportunities, stocks...). Both are needed: "is there a chance
#: (فرصة) the bot sells?" or "search for why buying is halted" are questions
#: about the bot, not scans.
_ASK = re.compile(r"(ابحث|دور ?(لي|على|علي)|دوّر|رشح|افضل|أفضل|احسن|أحسن|اقوى|أقوى|"
                  r"\b(find|search|scan|best|top|recommend)\b)", re.IGNORECASE)
_WHAT = re.compile(r"(فرص|سهم|اسهم|أسهم|opportunit|\bstocks?\b|\bshares?\b|\bset-?ups?\b)",
                   re.IGNORECASE)


def scan_request(question: str) -> Optional[int]:
    """How many results the operator asked for, when the question asks for a scan."""
    text = (question or "").translate(_ARABIC_DIGITS)
    if not (_ASK.search(text) and _WHAT.search(text)):
        return None
    number = re.search(r"\b(\d{1,2})\b", text)
    return max(1, min(int(number.group(1)), MAX_TOP)) if number else 5

"""Loading price history, and a synthetic generator for exercising the engine.

`load_csv` is the real path. `synthetic_history` exists only so the engine can be
tested and demonstrated without a data subscription, and it is labelled loudly
wherever it surfaces: **a backtest on synthetic prices measures the engine, not
the strategy.** Numbers produced from it say nothing about the EGX and must never
be quoted as results.
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from ..clock import TradingCalendar
from ..types import Tradability
from .types import Bar, MacroBar, PriceHistory

REQUIRED_COLUMNS = {"date", "symbol", "open", "high", "low", "close"}


class HistoryError(ValueError):
    """The history is unusable. Never silently repaired -- bad bars poison a run."""


def load_csv(
    path: str | Path,
    *,
    macro_path: Optional[str | Path] = None,
    symbols: Optional[Iterable[str]] = None,
) -> PriceHistory:
    """Load OHLCV bars from a CSV.

    Expected columns: ``date,symbol,open,high,low,close[,volume][,tradability]``
    with ISO dates. A separate ``date,usd_egp`` file supplies the macro series.

    Rows are validated rather than coerced: a bar whose low exceeds its high, or
    whose prices are non-positive, raises. Quietly "fixing" such a row would hide
    a data problem behind a plausible-looking result.
    """
    path = Path(path)
    wanted = set(symbols) if symbols else None
    bars: dict[date, dict[str, Bar]] = {}

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise HistoryError(f"{path}: missing columns {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            symbol = (row["symbol"] or "").strip().upper()
            if not symbol or (wanted and symbol not in wanted):
                continue
            try:
                day = date.fromisoformat((row["date"] or "").strip())
                bar = Bar(
                    day=day,
                    symbol=symbol,
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=Decimal(row.get("volume") or 0),
                    tradability=Tradability((row.get("tradability") or "open").strip() or "open"),
                )
            except (ValueError, ArithmeticError) as exc:
                raise HistoryError(f"{path}:{line}: {exc}") from exc
            bars.setdefault(day, {})[symbol] = bar

    if not bars:
        raise HistoryError(f"{path}: no usable rows")

    macro = _load_macro(macro_path) if macro_path else {}
    return PriceHistory(bars=bars, macro=macro, days=tuple(sorted(bars)))


def _load_macro(path: str | Path) -> dict[date, MacroBar]:
    out: dict[date, MacroBar] = {}
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            try:
                day = date.fromisoformat((row["date"] or "").strip())
                out[day] = MacroBar(day=day, usd_egp=Decimal(row["usd_egp"]))
            except (KeyError, ValueError, ArithmeticError) as exc:
                raise HistoryError(f"{path}:{line}: {exc}") from exc
    return out


SYNTHETIC_WARNING = (
    "SYNTHETIC PRICES -- these are randomly generated, not EGX data. A run on "
    "this history exercises the engine and the cost model only. Any return, "
    "drawdown or ranking it produces is meaningless as a statement about the "
    "market or the strategy."
)


def synthetic_history(
    symbols: Sequence[str],
    *,
    start: date,
    sessions: int = 500,
    seed: int = 20260922,
    annual_drift: float = 0.10,
    annual_vol: float = 0.30,
    devaluation_at: Optional[int] = None,
    calendar: Optional[TradingCalendar] = None,
) -> PriceHistory:
    """Generate a plausible-shaped random history. For testing the engine ONLY.

    Includes an optional step devaluation so the policy's two-state macro switch
    has something to react to, and thin volume on some names so the
    participation cap actually binds -- both of which exercise code paths a
    smooth random walk would never reach.
    """
    rng = random.Random(seed)
    calendar = calendar or TradingCalendar()

    days: list[date] = []
    cursor = start
    while len(days) < sessions:
        if calendar.is_session_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)

    daily_drift = annual_drift / 245
    daily_vol = annual_vol / (245 ** 0.5)

    levels = {s: 20.0 + rng.random() * 80.0 for s in symbols}
    # One deliberately thin name, so the volume cap is exercised.
    thin = symbols[-1] if symbols else None

    bars: dict[date, dict[str, Bar]] = {}
    macro: dict[date, MacroBar] = {}
    usd_egp = 48.0

    for index, day in enumerate(days):
        if devaluation_at is not None and index == devaluation_at:
            usd_egp *= 1.35  # a step repricing, as Egypt has repeatedly seen
        else:
            usd_egp *= 1 + rng.gauss(0.0002, 0.0008)
        macro[day] = MacroBar(day=day, usd_egp=Decimal(str(round(usd_egp, 4))))

        row: dict[str, Bar] = {}
        for symbol in symbols:
            shock = rng.gauss(daily_drift, daily_vol)
            previous = levels[symbol]
            level = max(0.5, previous * (1 + shock))
            levels[symbol] = level
            high = level * (1 + abs(rng.gauss(0, 0.006)))
            low = level * (1 - abs(rng.gauss(0, 0.006)))
            open_ = min(max(previous, low), high)
            volume = rng.uniform(800, 4000) if symbol == thin else rng.uniform(20_000, 200_000)
            row[symbol] = Bar(
                day=day,
                symbol=symbol,
                open=_d(open_),
                high=_d(max(high, level, open_)),
                low=_d(min(low, level, open_)),
                close=_d(level),
                volume=_d(volume, places="1"),
            )
        bars[day] = row

    return PriceHistory(bars=bars, macro=macro, days=tuple(days))


def _d(value: float, places: str = "0.001") -> Decimal:
    return Decimal(str(value)).quantize(Decimal(places))

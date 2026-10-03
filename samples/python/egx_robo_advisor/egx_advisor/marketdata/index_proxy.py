"""A stand-in for the EGX 30 index, built from the scan list's own prices.

Yahoo serves ^CASE30 for the last five sessions only (period "1d" or "5d"),
and no free source the app uses has its history. Everything that reads "the
market" -- the regime, MACRO_RISK_OFF, the paper account's comparison --
needs months of it, so the app builds its own:

    level(day) = level(day before) * (1 + the average of that day's moves)

over the stocks of config/scan_universe.toml (the largest EGX names, close to
the EGX 30's members), each stock weighted equally. A day counts only when at
least half of the stocks that had a price by then traded, and one stock's
move is held to +/-20% so one bad print cannot move the whole index.

It is not the official index: equal weights give the smaller names more say
than the EGX 30's market-cap weights do. Its direction and its large falls
are what the app uses it for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

#: Shown wherever the stand-in is reported, so it is never taken for the official index.
LABEL = "EGX 30 stand-in: equal-weight average of the scan list (Yahoo has no EGX 30 history)"
START_LEVEL = 1000.0
MAX_MOVE = 0.20
MIN_SHARE = 0.5


@dataclass(frozen=True)
class IndexBar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def build(rows: Mapping[str, Sequence[Any]]) -> list[IndexBar]:
    """Daily index bars, oldest first, from each stock's daily bars (anything with .day, .close)."""
    moves: dict[date, list[float]] = {}
    first_seen: dict[str, date] = {}
    for symbol, series in rows.items():
        previous = None
        for bar in sorted(series, key=lambda b: b.day):
            close = float(bar.close)
            if close <= 0:
                previous = None
                continue
            first_seen.setdefault(symbol, bar.day)
            if previous is not None:
                move = max(-MAX_MOVE, min(MAX_MOVE, close / previous - 1))
                moves.setdefault(bar.day, []).append(move)
            previous = close
    level, out = START_LEVEL, []
    for day in sorted(moves):
        listed = sum(1 for d in first_seen.values() if d < day)
        today = moves[day]
        if not listed or len(today) < max(1, listed * MIN_SHARE):
            continue
        level *= 1 + sum(today) / len(today)
        out.append(IndexBar(day, level, level, level, level))
    return out

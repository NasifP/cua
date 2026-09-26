"""A local archive of daily prices, so the app remembers what it has fetched.

Every history Yahoo returns is kept in state/memory/prices.db. Then:

- a second scan the same day reads the archive instead of fetching again;
- when Yahoo is down, the scan and the pick review still work from what was
  kept, and say the prices are from the archive;
- the pick review (review.py) has prices after each pick to measure against.

Archived prices are checked again when they are read, with the same checks as
fresh ones: a bar that was bad when fetched is still reported.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .base import MarketDataError, QualityPolicy
from .quality import validate_bars
from .yahoo import YahooRow

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT NOT NULL,
    PRIMARY KEY (symbol, day)
);
CREATE TABLE IF NOT EXISTS fetches (
    symbol TEXT PRIMARY KEY,
    fetched_on TEXT NOT NULL,
    since TEXT NOT NULL
);
"""


def archive_path_for(bus_file: str | Path) -> Path:
    return Path(bus_file).parent / "memory" / "prices.db"


def _cairo_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=2)).date()


class PriceArchive:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def store(self, rows: Mapping[str, Sequence[YahooRow]], fetched_on: date, since: date) -> int:
        stored = 0
        with closing(self._connect()) as conn, conn:
            for symbol, series in rows.items():
                if not series:
                    continue
                conn.executemany(
                    "INSERT OR REPLACE INTO bars VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(symbol, r.day.isoformat(), str(r.open), str(r.high), str(r.low),
                      str(r.close), str(r.volume)) for r in series])
                conn.execute("INSERT OR REPLACE INTO fetches VALUES (?, ?, ?)",
                             (symbol, fetched_on.isoformat(), since.isoformat()))
                stored += len(series)
        return stored

    def series(self, symbol: str, since: Optional[date] = None) -> list[YahooRow]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT day, open, high, low, close, volume FROM bars WHERE symbol = ? "
                "AND day >= ? ORDER BY day", (symbol, (since or date.min).isoformat())).fetchall()
        return [YahooRow(date.fromisoformat(d), Decimal(o), Decimal(h), Decimal(lo),
                         Decimal(c), Decimal(v)) for d, o, h, lo, c, v in rows]

    def fresh(self, symbols: Sequence[str], today: date, since: date) -> bool:
        """Every symbol was fetched today, from `since` or earlier."""
        with closing(self._connect()) as conn:
            rows = dict((s, (f, b)) for s, f, b in conn.execute(
                "SELECT symbol, fetched_on, since FROM fetches"))
        return all(
            s in rows and rows[s][0] == today.isoformat() and rows[s][1] <= since.isoformat()
            for s in symbols)


@dataclass
class ArchivedMarketData:
    """A market-data provider that keeps what it fetches, and falls back on it."""

    inner: Any
    archive: PriceArchive
    today: Callable[[], date] = _cairo_today
    quality: QualityPolicy = field(default_factory=QualityPolicy)
    #: True when the last history came from the archive because the fetch failed.
    stale: bool = False
    _report: Any = None

    @property
    def last_report(self) -> Any:
        return self._report

    async def history(self, universe: Sequence[Any], *, days: int = 1825
                      ) -> Mapping[str, Sequence[YahooRow]]:
        from .base import QualityReport

        symbols = [i.symbol for i in universe]
        today = self.today()
        since = today - timedelta(days=days)
        self.stale = False
        if self.archive.fresh(symbols, today, since):
            return self._from_archive(symbols, since, QualityReport(symbols_checked=len(symbols)))
        try:
            rows = await self.inner.history(universe, days=days)
        except MarketDataError as exc:
            logger.warning("fetch failed, using the price archive: %s", exc)
            archived = self._from_archive(symbols, since,
                                          QualityReport(symbols_checked=len(symbols)))
            if not any(archived.values()):
                raise
            self.stale = True
            return archived
        self.archive.store({s: rows.get(s) or () for s in symbols}, today, since)
        self._report = getattr(self.inner, "last_report", None)
        return rows

    def _from_archive(self, symbols: Sequence[str], since: date, report: Any
                      ) -> dict[str, list[YahooRow]]:
        rows = {s: self.archive.series(s, since) for s in symbols}
        for symbol, series in rows.items():
            report.extend(validate_bars(series, symbol, policy=self.quality).findings)
        self._report = report
        return rows

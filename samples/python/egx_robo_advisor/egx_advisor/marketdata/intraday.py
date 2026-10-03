"""Prices during the session, read from the Thndr X page the operator has open.

Yahoo has no intraday prices for EGX stocks, and the daily archive
(archive.py) only knows each session's close. The desktop app therefore reads
the text of the Thndr X page in its own window once a minute while the
exchange is open -- the same read the bridge already does, in an isolated
world, with no click and no navigation -- and keeps every price it can
recognise. A watchlist page shows many stocks at once; a stock page shows one.

Recognising a price on a page laid out for people is guesswork, so a number
is kept only when:

- it follows a known ticker within a few lines;
- it is written with decimals ("129.45"), as prices are -- not a quantity;
- it is not a percentage or a signed change ("+1.20", "-0.5%");
- on the positions page, it is not inside a table row: there a row's numbers
  are quantity, average cost and value, so that table is read by its columns
  instead (price = market value / quantity, `prices_from_positions`);
- it lies within 20% of the stock's last daily close, the EGX's widest daily
  limit band. Anything else is a volume, a change, a quantity -- not a price.

Kept in state/memory/intraday.db; one row per stock per minute.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

#: The widest EGX daily band: a price further than this from the last close is not one.
BAND = 0.20
#: Lines after a ticker that may still carry its price.
LOOKAHEAD = 4
#: A sampled minute counts toward a session; this many make a session "seen".
SESSION_MINUTES = 60
#: A price older than this is not "now".
FRESH_MINUTES = 20

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")
_NUMBER = re.compile(r"(?<![\w.+\-])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\d%]|\.\d|\s*%)")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    symbol TEXT NOT NULL,
    minute TEXT NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (symbol, minute)
);
"""


def intraday_path_for(bus_file: str | Path) -> Path:
    return Path(bus_file).parent / "memory" / "intraday.db"


def cairo_now() -> datetime:
    """Cairo wall-clock time, summer time included, without a time zone attached."""
    from ..clock import CAIRO

    return datetime.now(timezone.utc).astimezone(CAIRO).replace(tzinfo=None)


def _numbers(line: str) -> list[float]:
    """Prices on a line: with decimals, not signed, not followed by %, not in a word."""
    out = []
    for m in _NUMBER.finditer(line.translate(_ARABIC_DIGITS)):
        if not m.group(2):
            continue
        start = m.start()
        if start and line[start - 1] in "+-−":
            continue
        whole = m.group(1).replace(",", "")
        out.append(float(f"{whole}.{m.group(2)}" if m.group(2) else whole))
    return out


def parse_prices(text: str, reference: Mapping[str, float],
                 skip_table_rows: bool = False) -> dict[str, float]:
    """{symbol.CA: price} recognised on a page's text, given each stock's last close."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    found: dict[str, float] = {}
    tickers = {s.removesuffix(".CA").upper(): s for s in reference}
    for i, line in enumerate(lines):
        if skip_table_rows and line.count("\t") >= 2:
            continue  # a positions row: read by its columns, not guessed at
        words = set(re.findall(r"[A-Z][A-Z0-9]{2,6}", line.upper()))
        for ticker in words & set(tickers):
            symbol = tickers[ticker]
            if symbol in found:
                continue
            close = reference[symbol]
            if close <= 0:
                continue
            for candidate in lines[i:i + LOOKAHEAD + 1]:
                prices = [p for p in _numbers(candidate) if abs(p / close - 1) <= BAND]
                if prices:
                    found[symbol] = prices[0]
                    break
    return found



def prices_from_positions(text: str, reference: Mapping[str, float]) -> dict[str, float]:
    """Prices from the Thndr X positions table: market value / quantity, per row."""
    from ..execution.thndr import ThndrUiMap, parse_positions_table

    table = parse_positions_table(text or "", ThndrUiMap())
    out = {}
    for row in (table or {}).get("positions") or ():
        try:
            qty, value = float(row["quantity"]), float(row["market_value"])
        except (KeyError, TypeError, ValueError):
            continue
        close = reference.get(row["symbol"])
        if qty > 0 and close and abs(value / qty / close - 1) <= BAND:
            out[row["symbol"]] = round(value / qty, 3)
    return out


def read_page(text: str, reference: Mapping[str, float]) -> dict[str, float]:
    """Every price a Thndr X page shows: its positions table, then any other listing."""
    from ..execution.thndr import ThndrUiMap, parse_positions_table

    positions_page = parse_positions_table(text or "", ThndrUiMap()) is not None
    return {**parse_prices(text, reference, skip_table_rows=positions_page),
            **prices_from_positions(text, reference)}

@dataclass(frozen=True)
class Tick:
    symbol: str
    at: datetime  # Cairo time, minute precision
    price: float


class IntradayStore:
    """SQLite-backed, one short connection per call: safe from any thread."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def add(self, prices: Mapping[str, float], at: datetime) -> int:
        minute = at.replace(second=0, microsecond=0, tzinfo=None).isoformat(timespec="minutes")
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO ticks (symbol, minute, price) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol, minute) DO UPDATE SET price = excluded.price",
                [(s, minute, float(p)) for s, p in prices.items()])
        return len(prices)

    def ticks(self, symbol: str, since: Optional[datetime] = None) -> list[Tick]:
        start = (since or datetime.min).replace(tzinfo=None).isoformat(timespec="minutes")
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT minute, price FROM ticks WHERE symbol = ? AND "
                                "minute >= ? ORDER BY minute", (symbol, start)).fetchall()
        return [Tick(symbol, datetime.fromisoformat(m), p) for m, p in rows]

    def last(self, symbol: str, now: Optional[datetime] = None,
             fresh_minutes: int = FRESH_MINUTES) -> Optional[Tick]:
        """The latest price, if it is recent enough to call "now"."""
        now = (now or cairo_now()).replace(tzinfo=None)
        rows = self.ticks(symbol, now - timedelta(minutes=fresh_minutes))
        return rows[-1] if rows else None

    def days(self) -> list[date]:
        """Every day with at least one price read, oldest first."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT DISTINCT substr(minute, 1, 10) FROM ticks "
                                "ORDER BY 1").fetchall()
        return [date.fromisoformat(d) for (d,) in rows]

    def sessions(self, minutes: int = SESSION_MINUTES) -> list[date]:
        """Days with at least `minutes` sampled minutes in which some price moved."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT substr(minute, 1, 10) AS day, COUNT(DISTINCT minute), "
                "COUNT(DISTINCT symbol || ':' || price) - COUNT(DISTINCT symbol) "
                "FROM ticks GROUP BY day").fetchall()
        return [date.fromisoformat(d) for d, n, moved in rows if n >= minutes and moved > 0]


def reference_closes(archive: object, symbols: Iterable[str], today: date
                     ) -> dict[str, float]:
    """Each stock's last daily close before `today`, from the price archive."""
    out = {}
    since = today - timedelta(days=14)
    for symbol in symbols:
        try:
            rows = [r for r in archive.series(symbol, since) if r.day < today]  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - no prices: no reference, no reading
            continue
        if rows and float(rows[-1].close) > 0:
            out[symbol] = float(rows[-1].close)
    return out


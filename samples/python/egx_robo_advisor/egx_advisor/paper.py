"""The paper trading journal: virtual trades with a stop, a target and real costs.

No money moves and nothing is sent to Thndr X. A paper trade is written down
the moment the bot's plan wants to buy (or when the operator opens one on the
Paper trading page), and closed by the daily prices that followed:

- size: from the risk, not the cash. The shares are those whose loss at the
  stop is EGX_RISK_PER_TRADE_PCT of the paper account (default 1%), and no
  more than the free paper cash buys;
- exit: the first later session that reaches the stop or the target. A
  session that opens beyond either exits at its open (a gap). A session that
  reaches both is counted as a stop -- daily bars cannot tell which came
  first, so the journal assumes the worse;
- costs: the backtester's cost model (backtest/types.py) on both sides.

A trade closed by hand ("manual") counts against exit discipline on the Learn
tab. The paper account has its own daily loss limit and drawdown
(risk_limits.py): a breach pauses new paper trades.

Kept next to the memory (state/memory/memory.db), readable from any thread.
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import risk_limits as rl

DEFAULT_CAPITAL = 100_000.0
CAPITAL = ("EGX_PAPER_CAPITAL", DEFAULT_CAPITAL, (1_000.0, 100_000_000.0))
RULE_EXITS = ("stop", "target")
GUARD_KEY = "paper.guard"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    fees_in REAL NOT NULL,
    source TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    closed TEXT,
    exit REAL,
    fees_out REAL,
    exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS paper_equity (
    day TEXT PRIMARY KEY,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def capital_from(env: Mapping[str, str]) -> float:
    return rl._setting(env, CAPITAL)


def costs(price: float, quantity: int) -> float:
    """Commission, fees, duty and slippage for one side, from the backtester's model."""
    from .backtest.types import CostModel

    model = CostModel()
    notional = Decimal(str(price)) * quantity
    return float(model.charges(notional) + notional * model.slippage_rate)


@dataclass(frozen=True)
class Trade:
    id: int
    opened: date
    symbol: str
    quantity: int
    entry: float
    stop: float
    target: float
    fees_in: float
    source: str
    reason: str
    status: str
    closed: Optional[date] = None
    exit: Optional[float] = None
    fees_out: Optional[float] = None
    exit_reason: Optional[str] = None

    @property
    def risk(self) -> float:
        """What the trade loses at its stop, before costs."""
        return (self.entry - self.stop) * self.quantity

    def pnl(self, price: Optional[float] = None) -> float:
        """Result after costs: realised when closed, else marked at `price`."""
        if self.status == "closed" and self.exit is not None:
            return (self.exit - self.entry) * self.quantity - self.fees_in - (self.fees_out or 0)
        if price is None:
            return -self.fees_in
        return (price - self.entry) * self.quantity - self.fees_in


@dataclass(frozen=True)
class Stats:
    capital: float
    equity: float
    realised: float
    open_pnl: float
    closed: int
    wins: int
    rule_exits: int
    peak: float
    first_day: Optional[date]

    @property
    def win_rate(self) -> float:
        return self.wins / self.closed if self.closed else 0.0

    @property
    def drawdown_pct(self) -> float:
        return (self.peak - self.equity) / self.peak * 100 if self.peak > 0 else 0.0


def _day(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


class PaperBook:
    """SQLite-backed, one short connection per call: safe from any thread."""

    def __init__(self, path: str | Path, capital: float = DEFAULT_CAPITAL,
                 risk_pct: float = rl.RISK_PER_TRADE[1],
                 limits: Optional[rl.Limits] = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.capital = capital
        self.risk_pct = risk_pct
        self.limits = limits or rl.Limits()
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)

    @classmethod
    def from_env(cls, path: str | Path, env: Mapping[str, str]) -> "PaperBook":
        return cls(path, capital_from(env), rl.risk_per_trade_pct(env), rl.limits_from(env))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ------------------------------------------------------------------ reads

    def trades(self, status: Optional[str] = None) -> list[Trade]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, opened, symbol, quantity, entry, stop, target, fees_in, source, "
                "reason, status, closed, exit, fees_out, exit_reason FROM paper_trades"
                + (" WHERE status = ?" if status else "") + " ORDER BY id",
                (status,) if status else ()).fetchall()
        return [Trade(r[0], date.fromisoformat(r[1]), *r[2:11], _day(r[11]), *r[12:15])
                for r in rows]

    def guard(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM paper_state WHERE key = ?",
                               (GUARD_KEY,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _set_guard(self, state: Mapping[str, Any]) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO paper_state (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (GUARD_KEY, json.dumps(state)))

    def equity_curve(self) -> list[tuple[date, float]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT day, equity FROM paper_equity ORDER BY day").fetchall()
        return [(date.fromisoformat(d), e) for d, e in rows]

    def stats(self, marks: Mapping[str, float] | None = None) -> Stats:
        marks = marks or {}
        trades = self.trades()
        closed = [t for t in trades if t.status == "closed"]
        realised = sum(t.pnl() for t in closed)
        open_pnl = sum(t.pnl(marks.get(t.symbol)) for t in trades if t.status == "open")
        equity = self.capital + realised + open_pnl
        curve = [e for _, e in self.equity_curve()]
        return Stats(self.capital, equity, realised, open_pnl, len(closed),
                     sum(1 for t in closed if t.pnl() > 0),
                     sum(1 for t in closed if t.exit_reason in RULE_EXITS),
                     max([self.capital, equity, *curve]),
                     min((t.opened for t in trades), default=None))

    def paused(self, today: date) -> Optional[str]:
        return rl.paused(self.guard(), today.isoformat())

    # ------------------------------------------------------------------ writes

    def open_trade(self, symbol: str, entry: float, stop: float, target: float, *,
                   source: str, reason: str = "", today: date,
                   marks: Mapping[str, float] | None = None) -> Trade:
        """Write down a new paper trade, sized from the risk. Raises ValueError if refused."""
        symbol = symbol.strip().upper()
        if not all(math.isfinite(v) and v > 0 for v in (entry, stop, target)):
            raise ValueError("entry, stop and target must be positive prices")
        if not stop < entry < target:
            raise ValueError("the stop must be below the entry and the target above it")
        why = self.paused(today)
        if why:
            raise ValueError(f"paper trading is paused: {why} loss limit reached")
        trades = self.trades()
        if any(t.symbol == symbol and (t.status == "open" or t.opened == today) for t in trades):
            raise ValueError(f"{symbol} already has a paper trade today or still open")
        stats = self.stats(marks)
        per_share = entry - stop
        by_risk = math.floor(stats.equity * self.risk_pct / 100 / per_share)
        in_use = sum(t.entry * t.quantity + t.fees_in for t in trades if t.status == "open")
        free = self.capital + stats.realised - in_use
        by_cash = math.floor(free / (entry * 1.01)) if free > 0 else 0
        quantity = min(by_risk, by_cash)
        if quantity < 1:
            raise ValueError("not enough paper cash or risk budget for one share")
        fees = costs(entry, quantity)
        with closing(self._connect()) as conn, conn:
            trade_id = conn.execute(
                "INSERT INTO paper_trades (opened, symbol, quantity, entry, stop, target, "
                "fees_in, source, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (today.isoformat(), symbol, quantity, entry, stop, target, fees, source,
                 reason[:300])).lastrowid
        return next(t for t in self.trades("open") if t.id == trade_id)

    def _close(self, trade: Trade, day: date, price: float, why: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE paper_trades SET status = 'closed', closed = ?, exit = ?, fees_out = ?, "
                "exit_reason = ? WHERE id = ? AND status = 'open'",
                (day.isoformat(), price, costs(price, trade.quantity), why, trade.id))

    def close_by_hand(self, trade_id: int, price: float, today: date) -> None:
        """The operator closes a trade. Counts against exit discipline."""
        trade = next((t for t in self.trades("open") if t.id == trade_id), None)
        if trade is None:
            raise ValueError("no such open paper trade")
        if not (math.isfinite(price) and price > 0):
            raise ValueError("a positive price is needed to close")
        self._close(trade, today, price, "manual")

    def settle(self, bars_for: Callable[[str], Sequence[Any]], today: date,
               ticks_for: Optional[Callable[[str], Sequence[Any]]] = None
               ) -> tuple[list[Trade], Optional[rl.Breach]]:
        """Close what later sessions stopped out or took profit on; mark the rest.

        `bars_for(symbol)` gives daily bars (day, open, high, low, close), oldest
        first. `ticks_for(symbol)`, when given, gives prices read during sessions
        (marketdata/intraday.py: .at, .price), so a trade can close the same day
        rather than at the next daily bar. Returns the trades closed now and a
        new loss-limit breach, if any.
        """
        closed_now, marks = [], {}
        for trade in self.trades("open"):
            bars = [b for b in bars_for(trade.symbol) if b.day > trade.opened]
            if self._settle_bars(trade, bars):
                closed_now.append(trade)
                continue
            if bars:
                marks[trade.symbol] = float(bars[-1].close)
            after = max([trade.opened] + [b.day for b in bars])
            ticks = [t for t in (ticks_for(trade.symbol) if ticks_for else ())
                     if t.at.date() > after]
            if self._settle_ticks(trade, ticks):
                closed_now.append(trade)
            elif ticks:
                marks[trade.symbol] = float(ticks[-1].price)
        return closed_now, self._record_equity(marks, today)

    def _settle_ticks(self, trade: Trade, ticks: Sequence[Any]) -> bool:
        """The first sampled price at or past the stop or target closes the trade.

        A stop exits at the sampled price (it may already be below the stop);
        a target exits at the target, never better.
        """
        for tick in ticks:
            price = float(tick.price)
            if price <= trade.stop:
                self._close(trade, tick.at.date(), price, "stop")
                return True
            if price >= trade.target:
                self._close(trade, tick.at.date(), trade.target, "target")
                return True
        return False

    def _settle_bars(self, trade: Trade, bars: Sequence[Any]) -> bool:
        for bar in bars:
            o, h, lo = float(bar.open), float(bar.high), float(bar.low)
            exit_ = None
            if o <= trade.stop:
                exit_ = (o, "stop")
            elif lo <= trade.stop:
                exit_ = (trade.stop, "stop")
            elif o >= trade.target:
                exit_ = (o, "target")
            elif h >= trade.target:
                exit_ = (trade.target, "target")
            if exit_:
                self._close(trade, bar.day, *exit_)
                return True
        return False

    def _record_equity(self, marks: Mapping[str, float], today: date) -> Optional[rl.Breach]:
        equity = self.stats(marks).equity
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO paper_equity (day, equity) VALUES (?, ?) "
                         "ON CONFLICT(day) DO UPDATE SET equity = excluded.equity",
                         (today.isoformat(), equity))
        state, breach = rl.step(self.guard(), equity, today.isoformat(), self.limits)
        self._set_guard(state)
        return breach

    def resume(self, today: date, marks: Mapping[str, float] | None = None) -> None:
        """The operator resumes paper trading after a drawdown pause, accepting the loss."""
        state, _ = rl.step(self.guard(), self.stats(marks).equity, today.isoformat(),
                           self.limits, resumed=True)
        self._set_guard(state)


def open_from_levels(book: PaperBook, symbol: str, bars: Sequence[Any], price: float, *,
                     style: str, source: str, reason: str, today: date) -> Trade:
    """A paper trade whose stop and target come from levels.py, as for a holding."""
    from . import levels

    lv = levels.compute(symbol, bars, price, style)
    if lv is None:
        raise ValueError(f"not enough price history for {symbol} to place a stop")
    stop = min(lv.stop, price - lv.atr * 0.5)
    return book.open_trade(symbol, price, stop, lv.target1, source=source, reason=reason,
                           today=today)

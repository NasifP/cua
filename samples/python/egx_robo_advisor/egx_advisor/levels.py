"""Stop-loss and two targets for each holding, from the stock's own movement.

For every stock held, from daily bars up to the previous session and the
price Thndr X shows now:

- ATR: the average true range over 14 sessions, the stock's usual daily move
  in pounds (gaps included);
- stop: the highest high of the last 22 sessions minus a multiple of ATR (a
  "chandelier" stop). It only rises as the stock makes new highs, so a stock
  that keeps climbing drags its stop up behind it;
- target 1: the highest high of the last 60 sessions, the nearest resistance,
  when it is at least one ATR above the price; otherwise a multiple of ATR
  above the price;
- target 2: a larger multiple of ATR above the price.

The multiples come from the operator's style (Settings: EGX_STYLE): a trader
gives a stock little room and expects small moves, a long-term investor a lot
of room and larger ones.

These are levels to watch, not orders. The app shows where the price sits
between stop and target and warns when it is within one ATR of the stop or
below it. Nothing here sells anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

#: style -> (stop, target 1, target 2) in ATRs
STYLES: dict[str, tuple[float, float, float]] = {
    "trader": (1.5, 2.0, 4.0),   # days
    "swing": (2.5, 3.0, 6.0),    # weeks
    "long": (4.0, 5.0, 10.0),    # months
}
DEFAULT_STYLE = "swing"
ATR_DAYS = 14
STOP_LOOKBACK = 22
RESISTANCE_LOOKBACK = 60


@dataclass(frozen=True)
class Levels:
    symbol: str
    price: float
    atr: float
    stop: float
    target1: float
    target2: float
    avg_cost: Optional[float] = None

    @property
    def status(self) -> str:
        """"below_stop", "near_stop", "above_target1", "above_target2" or "ok"."""
        if self.price <= self.stop:
            return "below_stop"
        if self.price - self.stop <= self.atr:
            return "near_stop"
        if self.price >= self.target2:
            return "above_target2"
        if self.price >= self.target1:
            return "above_target1"
        return "ok"

    @property
    def position(self) -> float:
        """Where the price sits from stop (0) to target 2 (1), clamped."""
        span = self.target2 - self.stop
        return 0.0 if span <= 0 else max(0.0, min(1.0, (self.price - self.stop) / span))

    def to_json(self) -> dict[str, Any]:
        return {
            "price": round(self.price, 3), "atr": round(self.atr, 3),
            "stop": round(self.stop, 2), "target1": round(self.target1, 2),
            "target2": round(self.target2, 2), "status": self.status,
            "position": round(self.position, 3),
            "avg_cost": None if self.avg_cost is None else round(self.avg_cost, 3),
        }


def atr(rows: Sequence[Any], days: int = ATR_DAYS) -> Optional[float]:
    """Average true range over `days` sessions; None without enough bars."""
    if len(rows) < days + 1:
        return None
    window = rows[-(days + 1):]
    ranges = []
    for previous, bar in zip(window, window[1:], strict=False):
        high, low, prev_close = float(bar.high), float(bar.low), float(previous.close)
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    value = sum(ranges) / len(ranges)
    return value if value > 0 else None


def compute(symbol: str, rows: Sequence[Any], price: float, style: str = DEFAULT_STYLE,
            avg_cost: Optional[float] = None) -> Optional[Levels]:
    """Levels for one holding, or None without enough history or a price."""
    if price <= 0:
        return None
    k_stop, k1, k2 = STYLES.get(style, STYLES[DEFAULT_STYLE])
    rows = list(rows)
    move = atr(rows)
    if move is None:
        return None
    recent_high = max(float(r.high) for r in rows[-STOP_LOOKBACK:])
    stop = max(recent_high, price) - k_stop * move
    resistance = max(float(r.high) for r in rows[-RESISTANCE_LOOKBACK:])
    target1 = resistance if resistance >= price + move else price + k1 * move
    target2 = max(price + k2 * move, target1 + move)
    return Levels(symbol, price, move, stop, target1, target2, avg_cost)


def for_portfolio(positions: Sequence[Mapping[str, Any]],
                  history: Mapping[str, Sequence[Any]], style: str = DEFAULT_STYLE
                  ) -> dict[str, dict[str, Any]]:
    """Levels for every held position with a price and enough history."""
    out: dict[str, dict[str, Any]] = {}
    for p in positions:
        symbol = p["symbol"]
        quantity = float(p.get("quantity") or 0)
        value = float(p.get("market_value") or 0)
        if quantity <= 0 or value <= 0:
            continue
        cost = p.get("avg_cost")
        levels = compute(symbol, history.get(symbol) or (), value / quantity, style,
                         float(cost) if cost not in (None, "") else None)
        if levels is not None:
            out[symbol] = levels.to_json()
    return out


def style_from(env: Mapping[str, str]) -> str:
    value = (env.get("EGX_STYLE") or DEFAULT_STYLE).strip().lower()
    return value if value in STYLES else DEFAULT_STYLE


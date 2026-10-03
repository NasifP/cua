"""The daily loss limit and the maximum drawdown: when either is hit, stop.

One guard, used for two accounts:

- the real portfolio read from Thndr X each cycle: a breach HALTS the bot
  (egx_cua_agent.py), exactly as the halt button does;
- the paper book (paper.py): a breach pauses paper trading.

    daily loss = (the day's first value - value now) / the day's first value
    drawdown   = (the highest value seen - value now) / the highest value seen

Each breach fires once: the daily one once a day, the drawdown one until the
operator resumes. Resuming after a drawdown accepts the loss: the highest
value becomes the value at that moment, so the guard does not fire again at
once. The operator can always resume; the guard never resumes by itself.

What it cannot see: a withdrawal looks like a loss (and halts, which is the
safe side), and a drop before the day's first read is not counted. A read in
which the page showed no cash balance is not judged at all: the total would
swing with every sale.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: Defaults and the range each setting is held to, in percent.
DAILY_LOSS = ("EGX_DAILY_LOSS_PCT", 2.0, (0.5, 20.0))
MAX_DRAWDOWN = ("EGX_MAX_DRAWDOWN_PCT", 10.0, (2.0, 50.0))
RISK_PER_TRADE = ("EGX_RISK_PER_TRADE_PCT", 1.0, (0.1, 5.0))


def _setting(env: Mapping[str, str], spec: tuple[str, float, tuple[float, float]]) -> float:
    name, default, (low, high) = spec
    try:
        value = float(env.get(name) or default)
    except ValueError:
        value = default
    if not math.isfinite(value):
        value = default
    return max(low, min(high, value))


@dataclass(frozen=True)
class Limits:
    daily_pct: float = DAILY_LOSS[1]
    drawdown_pct: float = MAX_DRAWDOWN[1]


def limits_from(env: Mapping[str, str] = os.environ) -> Limits:
    return Limits(_setting(env, DAILY_LOSS), _setting(env, MAX_DRAWDOWN))


def risk_per_trade_pct(env: Mapping[str, str] = os.environ) -> float:
    """EGX_RISK_PER_TRADE_PCT: what one trade may lose at its stop, in % of the account."""
    return _setting(env, RISK_PER_TRADE)


@dataclass(frozen=True)
class Breach:
    #: "daily" or "drawdown"
    kind: str
    loss_pct: float
    limit_pct: float

    def describe(self) -> str:
        what = "daily loss" if self.kind == "daily" else "drawdown"
        return f"{what} {self.loss_pct:.1f}% reached the {self.limit_pct:.1f}% limit"


def step(state: Optional[Mapping[str, Any]], value: float, day: str, limits: Limits,
         resumed: bool = False) -> tuple[dict[str, Any], Optional[Breach]]:
    """The guard's next state after reading `value` on `day`, and a new breach if any.

    `state` is what the last call returned (None the first time); keep it and
    pass it back. `resumed`: the operator resumed after the last breach.
    """
    state = dict(state or {})
    fired = dict(state.get("fired") or {})
    if value <= 0 or not math.isfinite(value):
        return state, None  # no account value: nothing to judge
    if state.get("day") != day:
        state["day"], state["start"] = day, value
    if resumed and "drawdown" in fired:
        fired.pop("drawdown")
        state["peak"] = value
    peak = max(float(state.get("peak") or 0.0), value)
    state["peak"] = peak
    state["value"] = value

    breach = None
    start = float(state["start"])
    daily = (start - value) / start * 100
    drawdown = (peak - value) / peak * 100
    state["daily_pct"], state["drawdown_pct"] = round(daily, 3), round(drawdown, 3)
    if drawdown >= limits.drawdown_pct and "drawdown" not in fired:
        fired["drawdown"] = day
        breach = Breach("drawdown", drawdown, limits.drawdown_pct)
    elif daily >= limits.daily_pct and fired.get("daily") != day:
        fired["daily"] = day
        breach = Breach("daily", daily, limits.daily_pct)
    state["fired"] = fired
    return state, breach


def paused(state: Optional[Mapping[str, Any]], day: str) -> Optional[str]:
    """Why new trades are refused now ("daily" or "drawdown"), or None."""
    fired = (state or {}).get("fired") or {}
    if "drawdown" in fired:
        return "drawdown"
    if fired.get("daily") == day:
        return "daily"
    return None

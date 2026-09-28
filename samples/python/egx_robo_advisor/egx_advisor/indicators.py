"""Technical indicators the operator teaches the app, and what they did on the EGX.

The operator picks the indicators they use and their settings (Training tab).
The app then:

- draws them on the Chart tab (TradingView's own studies);
- reads them for every stock the analyst reviews (`readings`), so a view
  says where RSI, MACD, the bands and the averages stand;
- studies them: for every stock in the scan list, over the daily history,
  finds each indicator's buy and sell signals and measures what the price did
  5 and 20 sessions later, against the market's ordinary move over the same
  sessions (`study`). The result says, from the EGX's own past, whether an
  indicator has helped, misled or made no difference, and on which stocks.

Everything here is arithmetic over daily bars; no model is involved. A study
is history, not a promise: signals overlap, fees are not counted, and a few
dozen signals are a small sample. The verdicts say so.

No look-ahead: a signal on day i uses bars up to and including day i; the
trade it implies starts at the next session's open (or close when there is no
open), and the return runs from there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

HORIZONS = (5, 20)
#: Fewer buy signals than this across the whole list: "too few to say".
MIN_SIGNALS = 20
#: Average 20-session edge over the market, in percent, that counts.
EDGE_PCT = 1.0


@dataclass(frozen=True)
class Spec:
    key: str
    #: Parameter names and defaults, in the order the Training tab shows them.
    params: tuple[tuple[str, float], ...]
    #: TradingView chart-widget study id.
    tv_study: str


CATALOG: dict[str, Spec] = {s.key: s for s in (
    Spec("sma", (("length", 50),), "MASimple@tv-basicstudies"),
    Spec("ema_cross", (("fast", 20), ("slow", 50)), "MAExp@tv-basicstudies"),
    Spec("rsi", (("length", 14), ("low", 30), ("high", 70)), "RSI@tv-basicstudies"),
    Spec("macd", (("fast", 12), ("slow", 26), ("signal", 9)), "MACD@tv-basicstudies"),
    Spec("bollinger", (("length", 20), ("width", 2)), "BB@tv-basicstudies"),
    Spec("stochastic", (("k", 14), ("d", 3), ("low", 20), ("high", 80)),
         "Stochastic@tv-basicstudies"),
    Spec("volume", (("length", 20), ("ratio", 1.5)), "Volume@tv-basicstudies"),
)}
#: What a new operator starts with: what most EGX traders look at.
DEFAULT_ON = ("sma", "rsi", "macd")


# --------------------------------------------------------------------------- #
# The operator's set
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Choice:
    key: str
    on: bool
    params: Mapping[str, float] = field(default_factory=dict)

    def value(self, name: str) -> float:
        default = dict(CATALOG[self.key].params)[name]
        return float(self.params.get(name, default))

    def label(self) -> str:
        return f"{self.key}({', '.join(_fmt(self.value(n)) for n, _ in CATALOG[self.key].params)})"


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def default_set() -> list[Choice]:
    return [Choice(k, k in DEFAULT_ON, dict(s.params)) for k, s in CATALOG.items()]


def check_params(key: str, params: Mapping[str, Any]) -> dict[str, float]:
    """Clean parameters for one indicator; raises ValueError with a readable reason."""
    spec = CATALOG[key]
    out = {}
    for name, default in spec.params:
        raw = params.get(name, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key}: {name} must be a number") from None
        if name in ("length", "fast", "slow", "signal", "k", "d"):
            if value != int(value) or not 2 <= value <= 250:
                raise ValueError(f"{key}: {name} must be a whole number from 2 to 250")
        elif name in ("low", "high"):
            if not 1 <= value <= 99:
                raise ValueError(f"{key}: {name} must be between 1 and 99")
        elif not 0.1 <= value <= 10:
            raise ValueError(f"{key}: {name} must be between 0.1 and 10")
        out[name] = value
    if "fast" in out and "slow" in out and out["fast"] >= out["slow"]:
        raise ValueError(f"{key}: fast must be shorter than slow")
    if "low" in out and "high" in out and out["low"] >= out["high"]:
        raise ValueError(f"{key}: low must be below high")
    return out


def dump_set(choices: Iterable[Choice]) -> str:
    return json.dumps([{"key": c.key, "on": c.on, "params": dict(c.params)} for c in choices])


def load_set(text: Optional[str]) -> list[Choice]:
    """The saved set, completed with any catalog indicator it lacks; bad entries dropped."""
    try:
        raw = json.loads(text) if text else None
    except ValueError:
        raw = None
    if not isinstance(raw, list):
        return default_set()
    chosen: dict[str, Choice] = {}
    for entry in raw:
        if not isinstance(entry, dict) or entry.get("key") not in CATALOG:
            continue
        try:
            params = check_params(entry["key"], entry.get("params") or {})
        except ValueError:
            params = dict(CATALOG[entry["key"]].params)
        chosen[entry["key"]] = Choice(entry["key"], bool(entry.get("on")), params)
    return [chosen.get(k) or Choice(k, False, dict(s.params)) for k, s in CATALOG.items()]


def tv_studies(choices: Iterable[Choice]) -> list[str]:
    seen: list[str] = []
    for c in choices:
        study = CATALOG[c.key].tv_study
        if c.on and study not in seen:
            seen.append(study)
    return seen


# --------------------------------------------------------------------------- #
# The arithmetic. Lists in, lists out, None where a value is not defined yet.
# --------------------------------------------------------------------------- #

Series = list[Optional[float]]


def sma(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    value = sum(values[:n]) / n
    out[n - 1] = value
    for i in range(n, len(values)):
        value = values[i] * k + value * (1 - k)
        out[i] = value
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    """Wilder's RSI."""
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    gain, loss = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            gain = (gain * (n - 1) + gains[i - 1]) / n
            loss = (loss * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[Series, Series]:
    f, s = ema(closes, fast), ema(closes, slow)
    line: Series = [a - b if a is not None and b is not None else None for a, b in zip(f, s)]
    start = next((i for i, v in enumerate(line) if v is not None), len(line))
    tail = ema([v for v in line[start:] if v is not None], signal)
    return line, [None] * start + tail


def bollinger(closes: Sequence[float], n: int = 20, width: float = 2.0
              ) -> tuple[Series, Series, Series]:
    mid = sma(closes, n)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1:i + 1]
        mean = mid[i]
        sd = (sum((x - mean) ** 2 for x in window) / n) ** 0.5
        upper[i], lower[i] = mean + width * sd, mean - width * sd
    return mid, upper, lower


def stochastic(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               k: int = 14, d: int = 3) -> tuple[Series, Series]:
    pct_k: Series = [None] * len(closes)
    for i in range(k - 1, len(closes)):
        hi, lo = max(highs[i - k + 1:i + 1]), min(lows[i - k + 1:i + 1])
        pct_k[i] = 50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100
    start = k - 1
    tail = sma([v for v in pct_k[start:] if v is not None], d) if len(closes) > start else []
    return pct_k, [None] * start + tail


# --------------------------------------------------------------------------- #
# Signals and readings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Bars:
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    volumes: list[float]

    @classmethod
    def of(cls, rows: Sequence[Any]) -> "Bars":
        closes = [float(r.close) for r in rows]
        opens = [float(getattr(r, "open", 0) or 0) or c for r, c in zip(rows, closes)]
        return cls(opens, [float(r.high) for r in rows], [float(r.low) for r in rows], closes,
                   [float(r.volume or 0) for r in rows])


def _crossed_up(a: Series, b: Series, i: int) -> bool:
    return None not in (a[i - 1], b[i - 1], a[i], b[i]) and a[i - 1] <= b[i - 1] and a[i] > b[i]


def _crossed_down(a: Series, b: Series, i: int) -> bool:
    return None not in (a[i - 1], b[i - 1], a[i], b[i]) and a[i - 1] >= b[i - 1] and a[i] < b[i]


def signals(choice: Choice, bars: Bars) -> list[tuple[int, str]]:
    """(day index, "buy" or "sell") for every signal the indicator gives."""
    c, v = bars.closes, choice.value
    n = len(c)
    out: list[tuple[int, str]] = []
    price: Series = list(c)
    if choice.key == "sma":
        line = sma(c, int(v("length")))
        pairs = (price, line)
    elif choice.key == "ema_cross":
        pairs = (ema(c, int(v("fast"))), ema(c, int(v("slow"))))
    elif choice.key == "macd":
        pairs = macd(c, int(v("fast")), int(v("slow")), int(v("signal")))
    else:
        pairs = None
    if pairs is not None:
        a, b = pairs
        for i in range(1, n):
            if _crossed_up(a, b, i):
                out.append((i, "buy"))
            elif _crossed_down(a, b, i):
                out.append((i, "sell"))
        return out
    if choice.key == "rsi":
        r = rsi(c, int(v("length")))
        low, high = v("low"), v("high")
        for i in range(1, n):
            if r[i - 1] is None or r[i] is None:
                continue
            if r[i - 1] < low <= r[i]:
                out.append((i, "buy"))
            elif r[i - 1] > high >= r[i]:
                out.append((i, "sell"))
    elif choice.key == "bollinger":
        _, upper, lower = bollinger(c, int(v("length")), v("width"))
        for i in range(1, n):
            if None in (upper[i - 1], lower[i - 1], upper[i], lower[i]):
                continue
            if c[i - 1] < lower[i - 1] and c[i] >= lower[i]:
                out.append((i, "buy"))
            elif c[i - 1] > upper[i - 1] and c[i] <= upper[i]:
                out.append((i, "sell"))
    elif choice.key == "stochastic":
        k, d = stochastic(bars.highs, bars.lows, c, int(v("k")), int(v("d")))
        for i in range(1, n):
            if _crossed_up(k, d, i) and k[i - 1] < v("low"):
                out.append((i, "buy"))
            elif _crossed_down(k, d, i) and k[i - 1] > v("high"):
                out.append((i, "sell"))
    elif choice.key == "volume":
        avg = sma(bars.volumes, int(v("length")))
        for i in range(1, n):
            if avg[i - 1] and bars.volumes[i] > v("ratio") * avg[i - 1]:
                out.append((i, "buy" if c[i] > c[i - 1] else "sell"))
    return out


def _r(x: Optional[float], digits: int = 2) -> Optional[float]:
    return None if x is None else round(x, digits)


def reading(choice: Choice, bars: Bars) -> dict[str, Any]:
    """Where the indicator stands on the last bar, and its latest signal."""
    c, v = bars.closes, choice.value
    out: dict[str, Any] = {"indicator": choice.label()}
    if not c:
        return out
    last = c[-1]
    if choice.key == "sma":
        line = sma(c, int(v("length")))[-1]
        out.update(value=_r(line, 3), price_above=None if line is None else last > line,
                   distance_pct=_r((last / line - 1) * 100) if line else None)
    elif choice.key == "ema_cross":
        f, s = ema(c, int(v("fast")))[-1], ema(c, int(v("slow")))[-1]
        out.update(fast=_r(f, 3), slow=_r(s, 3), fast_above=None if None in (f, s) else f > s)
    elif choice.key == "rsi":
        r = rsi(c, int(v("length")))[-1]
        zone = None if r is None else ("oversold" if r < v("low") else
                                       "overbought" if r > v("high") else "neutral")
        out.update(value=_r(r, 1), zone=zone)
    elif choice.key == "macd":
        line, sig = macd(c, int(v("fast")), int(v("slow")), int(v("signal")))
        m, s = line[-1], sig[-1]
        out.update(macd=_r(m, 3), signal=_r(s, 3),
                   histogram=None if None in (m, s) else _r(m - s, 3),
                   above_signal=None if None in (m, s) else m > s)
    elif choice.key == "bollinger":
        mid, upper, lower = bollinger(c, int(v("length")), v("width"))
        u, lo = upper[-1], lower[-1]
        out.update(middle=_r(mid[-1], 3), upper=_r(u, 3), lower=_r(lo, 3),
                   position_pct=None if None in (u, lo) or u == lo
                   else _r((last - lo) / (u - lo) * 100, 1))
    elif choice.key == "stochastic":
        k, d = stochastic(bars.highs, bars.lows, c, int(v("k")), int(v("d")))
        out.update(k=_r(k[-1], 1), d=_r(d[-1], 1))
    elif choice.key == "volume":
        avg = sma(bars.volumes, int(v("length")))[-1]
        out.update(ratio_to_average=_r(bars.volumes[-1] / avg) if avg else None)
    found = signals(choice, bars)
    if found:
        i, side = found[-1]
        out["last_signal"] = {"side": side, "sessions_ago": len(c) - 1 - i}
    return out


# --------------------------------------------------------------------------- #
# The study
# --------------------------------------------------------------------------- #


def _forward(bars: Bars, i: int, h: int) -> Optional[float]:
    """Percent return from the next session's open to the close h sessions after day i."""
    if i + h >= len(bars.closes):
        return None
    entry = bars.opens[i + 1] or bars.closes[i + 1]
    return (bars.closes[i + h] / entry - 1) * 100 if entry else None


def _avg(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def study(choice: Choice, history: Mapping[str, Sequence[Any]],
          horizons: Sequence[int] = HORIZONS) -> dict[str, Any]:
    """What followed the indicator's signals across `history`, against the market's usual move."""
    far = max(horizons)
    buys: dict[int, list[float]] = {h: [] for h in horizons}
    sells: dict[int, list[float]] = {h: [] for h in horizons}
    base: dict[int, list[float]] = {h: [] for h in horizons}
    per_symbol: dict[str, list[float]] = {}
    sessions = 0
    for symbol, rows in history.items():
        bars = Bars.of(rows)
        n = len(bars.closes)
        if n < 60:
            continue
        sessions += n
        for h in horizons:
            base[h] += [r for r in (_forward(bars, i, h) for i in range(n - 1)) if r is not None]
        for i, side in signals(choice, bars):
            for h in horizons:
                r = _forward(bars, i, h)
                if r is not None:
                    (buys if side == "buy" else sells)[h].append(r)
            if side == "buy":
                r = _forward(bars, i, far)
                if r is not None:
                    per_symbol.setdefault(symbol, []).append(r)

    result: dict[str, Any] = {"indicator": choice.label(), "key": choice.key,
                              "params": dict(choice.params), "symbols": len(history),
                              "sessions": sessions}
    for h in horizons:
        market = _avg(base[h])
        b, s = buys[h], sells[h]
        result[f"h{h}"] = {
            "buy_signals": len(b),
            "buy_avg_pct": _r(_avg(b)),
            "buy_hit_pct": _r(sum(1 for x in b if x > 0) / len(b) * 100, 1) if b else None,
            "sell_signals": len(s),
            "sell_avg_pct": _r(_avg(s)),
            "market_avg_pct": _r(market),
            "market_hit_pct": _r(sum(1 for x in base[h] if x > 0) / len(base[h]) * 100, 1)
            if base[h] else None,
            "buy_edge_pct": _r(_avg(b) - market) if b and market is not None else None,
            "sell_edge_pct": _r(market - _avg(s)) if s and market is not None else None,
        }
    ranked = sorted(((sym, _avg(xs), len(xs)) for sym, xs in per_symbol.items() if len(xs) >= 3),
                    key=lambda t: -(t[1] or 0))
    result["best_symbols"] = [{"symbol": s, f"avg_{far}d_pct": _r(a), "signals": k}
                              for s, a, k in ranked[:5]]
    result["worst_symbols"] = [{"symbol": s, f"avg_{far}d_pct": _r(a), "signals": k}
                               for s, a, k in ranked[-3:][::-1] if (a or 0) < 0]
    result["verdict"] = verdict(result, far)
    return result


def verdict(result: Mapping[str, Any], horizon: int = max(HORIZONS)) -> str:
    """"too_few", "helped", "misled" or "no_edge", from the buy signals at `horizon`."""
    h = result.get(f"h{horizon}") or {}
    if (h.get("buy_signals") or 0) < MIN_SIGNALS or h.get("buy_edge_pct") is None:
        return "too_few"
    edge = h["buy_edge_pct"]
    if edge >= EDGE_PCT and (h.get("buy_hit_pct") or 0) >= (h.get("market_hit_pct") or 0):
        return "helped"
    if edge <= -EDGE_PCT:
        return "misled"
    return "no_edge"


def for_symbol(choices: Iterable[Choice], rows: Sequence[Any]) -> list[dict[str, Any]]:
    """Readings for every chosen indicator on one stock's bars."""
    bars = Bars.of(rows)
    return [reading(c, bars) for c in choices if c.on]

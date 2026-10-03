"""Market conditions, read from prices: regime, indicator weights, liquidity, risk-off.

Code, not a model, decides all of this, from daily bars up to the previous
session. The analyst's tools pass the results to the committee, so each
specialist sees the same numbers.

Regime (`detect_market_regime`)
-------------------------------
Uptrend, downtrend or sideways, for EGX30 or one stock:

- trend strength: the 50-day average's slope over 20 sessions, and where
  the close sits against that average;
- direction quality: the efficiency ratio over 20 sessions (net move divided
  by the sum of daily moves; 1 is a straight line, near 0 is chop).

A trend needs a sloping average, the close on the same side of it and an
efficiency ratio of at least ER_TREND. Everything else is sideways.

Dynamic weights (`weigh_readings`)
----------------------------------
Oscillators (RSI, stochastic, Bollinger) are right in a range and early in a
trend; trend followers (moving averages, EMA cross, MACD) are the other way
round. REGIME_WEIGHTS boosts the family that suits the regime and damps the
other. The operator's own study (indicators.verdict) still counts: an
indicator that misled on the EGX keeps a low weight whatever the regime.

Liquidity (`liquidity_cap`)
---------------------------
A buy may not exceed EGX_ADTV_PCT (default 3%) of the stock's average daily
traded volume over 20 sessions, whatever the cash. A position that is a large
share of a day's volume cannot be left quickly.

Macro risk-off (`macro_risk_off`)
---------------------------------
True when EGX30 falls hard (one session or five), or the news layer has
halted buying. While it holds, stops tighten (levels.compute(risk_off=True))
and the risk manager recommends raising cash / trimming positions.
"""

from __future__ import annotations

import enum
import math
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

# --------------------------------------------------------------------------- #
# Regime
# --------------------------------------------------------------------------- #

TREND_DAYS = 50
SLOPE_DAYS = 20
ER_DAYS = 20
#: Slope of the 50-day average over SLOPE_DAYS, %, that counts as a trend.
SLOPE_PCT = 1.0
#: Efficiency ratio at or above which a move is a trend rather than chop.
ER_TREND = 0.30


class Regime(str, enum.Enum):
    UPTREND = "uptrend"
    DOWNTREND = "downtrend"
    SIDEWAYS = "sideways"
    #: Not enough history to say.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RegimeReading:
    regime: Regime
    slope_pct: Optional[float] = None
    efficiency: Optional[float] = None
    close_vs_average_pct: Optional[float] = None

    def to_json(self) -> dict[str, Any]:
        return {"regime": self.regime.value, "sma50_slope_pct": _r(self.slope_pct),
                "efficiency_ratio": _r(self.efficiency), "close_vs_sma50_pct":
                _r(self.close_vs_average_pct)}


def _r(x: Optional[float], digits: int = 2) -> Optional[float]:
    return None if x is None else round(x, digits)


def _closes(price_data: Sequence[Any]) -> list[float]:
    """Closes from bars (anything with .close) or from plain numbers."""
    return [float(getattr(p, "close", p)) for p in price_data]


def efficiency_ratio(closes: Sequence[float], days: int = ER_DAYS) -> Optional[float]:
    if len(closes) < days + 1:
        return None
    window = closes[-(days + 1):]
    path = sum(abs(b - a) for a, b in zip(window, window[1:], strict=False))
    return abs(window[-1] - window[0]) / path if path > 0 else 0.0


def detect_market_regime(price_data: Sequence[Any]) -> RegimeReading:
    """The regime of EGX30 or one stock, from daily bars or closes, oldest first."""
    closes = _closes(price_data)
    if len(closes) < TREND_DAYS + SLOPE_DAYS:
        return RegimeReading(Regime.UNKNOWN)
    avg_now = sum(closes[-TREND_DAYS:]) / TREND_DAYS
    earlier = closes[:-SLOPE_DAYS]
    avg_then = sum(earlier[-TREND_DAYS:]) / TREND_DAYS
    if avg_now <= 0 or avg_then <= 0:
        return RegimeReading(Regime.UNKNOWN)
    slope = (avg_now / avg_then - 1) * 100
    er = efficiency_ratio(closes) or 0.0
    vs_avg = (closes[-1] / avg_now - 1) * 100
    if er >= ER_TREND and slope >= SLOPE_PCT and vs_avg > 0:
        regime = Regime.UPTREND
    elif er >= ER_TREND and slope <= -SLOPE_PCT and vs_avg < 0:
        regime = Regime.DOWNTREND
    else:
        regime = Regime.SIDEWAYS
    return RegimeReading(regime, slope, er, vs_avg)


# --------------------------------------------------------------------------- #
# Dynamic indicator weights
# --------------------------------------------------------------------------- #

OSCILLATORS = frozenset({"rsi", "stochastic", "bollinger"})
TREND_FOLLOWERS = frozenset({"sma", "ema_cross", "macd"})

#: regime -> indicator key -> multiplier. Missing keys weigh 1.0.
REGIME_WEIGHTS: dict[Regime, dict[str, float]] = {
    Regime.SIDEWAYS: {**{k: 1.5 for k in OSCILLATORS}, **{k: 0.75 for k in TREND_FOLLOWERS}},
    Regime.UPTREND: {**{k: 1.5 for k in TREND_FOLLOWERS}, **{k: 0.75 for k in OSCILLATORS}},
    # Falling markets: follow the trend, distrust "oversold" (it stays oversold).
    Regime.DOWNTREND: {**{k: 1.25 for k in TREND_FOLLOWERS}, **{k: 0.75 for k in OSCILLATORS}},
    Regime.UNKNOWN: {},
}
#: indicators.verdict -> multiplier: the operator's EGX study on top of the regime.
STUDY_WEIGHTS = {"helped": 1.25, "no_edge": 0.8, "misled": 0.5, "too_few": 1.0}


def _key(reading: Mapping[str, Any]) -> str:
    return str(reading.get("indicator", "")).split("(", 1)[0]


def direction(reading: Mapping[str, Any], choice_params: Optional[Mapping[str, float]] = None
              ) -> int:
    """+1 bullish, -1 bearish, 0 neutral or unknown, from one indicators.reading()."""
    key, p = _key(reading), dict(choice_params or {})

    def flag(value: Any) -> int:
        return 0 if value is None else (1 if value else -1)

    if key == "sma":
        return flag(reading.get("price_above"))
    if key == "ema_cross":
        return flag(reading.get("fast_above"))
    if key == "macd":
        return flag(reading.get("above_signal"))
    if key == "rsi":
        return {"oversold": 1, "overbought": -1}.get(reading.get("zone") or "", 0)
    if key == "bollinger":
        pos = reading.get("position_pct")
        return 0 if pos is None else (1 if pos <= 20 else -1 if pos >= 80 else 0)
    if key == "stochastic":
        k = reading.get("k")
        low, high = p.get("low", 20.0), p.get("high", 80.0)
        return 0 if k is None else (1 if k <= low else -1 if k >= high else 0)
    return 0  # volume confirms a move, it has no direction of its own


def weigh_readings(readings: Iterable[Mapping[str, Any]], regime: Regime,
                   study: Optional[Mapping[str, str]] = None,
                   params: Optional[Mapping[str, Mapping[str, float]]] = None
                   ) -> dict[str, Any]:
    """Each reading with its weight and weighted score, and the combined score.

    `study` maps an indicator label (indicators.Choice.label()) to its verdict.
    The combined score is the weighted average direction, from -1 (all bearish)
    to +1 (all bullish).
    """
    table = REGIME_WEIGHTS.get(regime, {})
    weighed, total, weights = [], 0.0, 0.0
    for reading in readings:
        key = _key(reading)
        verdict = (study or {}).get(str(reading.get("indicator", "")), "too_few")
        weight = table.get(key, 1.0) * STUDY_WEIGHTS.get(verdict, 1.0)
        d = direction(reading, (params or {}).get(key))
        weighed.append({**reading, "direction": d, "regime_weight": table.get(key, 1.0),
                        "study_verdict": verdict, "weight": round(weight, 3),
                        "score": round(d * weight, 3)})
        if key != "volume":
            total += d * weight
            weights += weight
    return {"regime": regime.value, "indicators": weighed,
            "combined_score": round(total / weights, 3) if weights else None}


# --------------------------------------------------------------------------- #
# Liquidity-aware sizing
# --------------------------------------------------------------------------- #

ADTV_DAYS = 20
DEFAULT_ADTV_PCT = 3.0
#: The setting is clamped to this range, in percent.
ADTV_PCT_RANGE = (0.5, 10.0)


def adtv_pct(env: Mapping[str, str] = os.environ) -> float:
    """EGX_ADTV_PCT: the most of a day's volume one buy may be, in percent."""
    try:
        value = float(env.get("EGX_ADTV_PCT") or DEFAULT_ADTV_PCT)
    except ValueError:
        value = DEFAULT_ADTV_PCT
    if not math.isfinite(value):
        value = DEFAULT_ADTV_PCT
    return max(ADTV_PCT_RANGE[0], min(ADTV_PCT_RANGE[1], value))


def adtv(volumes: Sequence[Any], days: int = ADTV_DAYS) -> Optional[float]:
    """Average daily traded volume (shares) over the last `days` sessions."""
    vols = [float(getattr(v, "volume", v) or 0) for v in volumes][-days:]
    if len(vols) < days:
        return None
    value = sum(vols) / days
    return value if value > 0 else None


@dataclass(frozen=True)
class LiquidityCap:
    adtv_shares: Optional[float]
    pct: float
    #: Most shares one buy may be; None when volume is unknown (then no buy passes).
    max_shares: Optional[int]

    def cap(self, wanted: int) -> tuple[int, bool]:
        """(shares allowed, whether the cap cut it)."""
        if self.max_shares is None:
            return 0, wanted > 0
        allowed = min(int(wanted), self.max_shares)
        return max(allowed, 0), allowed < wanted

    def to_json(self) -> dict[str, Any]:
        return {"adtv_20d_shares": None if self.adtv_shares is None else round(self.adtv_shares),
                "max_pct_of_adtv": self.pct, "max_shares": self.max_shares}


def liquidity_cap(volumes: Sequence[Any], env: Mapping[str, str] = os.environ,
                  pct: Optional[float] = None) -> LiquidityCap:
    """How many shares one buy may be, from the last 20 sessions' volume.

    Unknown volume fails closed: max_shares 0, not unlimited.
    """
    share = adtv_pct(env) if pct is None else pct
    average = adtv(volumes)
    if average is None:
        return LiquidityCap(None, share, 0)
    return LiquidityCap(average, share, int(math.floor(average * share / 100)))


# --------------------------------------------------------------------------- #
# Macro risk-off
# --------------------------------------------------------------------------- #

#: EGX30 falls that count as extreme stress, in percent.
DROP_1D_PCT = -4.0
DROP_5D_PCT = -7.0
#: The regime filter's states that mean "no new exposure".
STRESSED_STATES = frozenset({"buys_halted", "all_halted"})


@dataclass(frozen=True)
class MacroState:
    risk_off: bool
    reasons: list[str] = field(default_factory=list)
    egx30_1d_pct: Optional[float] = None
    egx30_5d_pct: Optional[float] = None

    def to_json(self) -> dict[str, Any]:
        return {"MACRO_RISK_OFF": self.risk_off, "reasons": self.reasons,
                "egx30_1d_pct": _r(self.egx30_1d_pct), "egx30_5d_pct": _r(self.egx30_5d_pct)}


def macro_risk_off(egx30: Sequence[Any], regime_snapshot: Optional[Mapping[str, Any]] = None
                   ) -> MacroState:
    """MACRO_RISK_OFF from EGX30's recent falls and the news layer's state."""
    closes = _closes(egx30)
    reasons: list[str] = []
    d1 = (closes[-1] / closes[-2] - 1) * 100 if len(closes) > 1 and closes[-2] else None
    d5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) > 5 and closes[-6] else None
    if d1 is not None and d1 <= DROP_1D_PCT:
        reasons.append(f"EGX30 fell {d1:.1f}% in one session")
    if d5 is not None and d5 <= DROP_5D_PCT:
        reasons.append(f"EGX30 fell {d5:.1f}% in five sessions")
    state = str((regime_snapshot or {}).get("risk_state") or "")
    if state in STRESSED_STATES:
        reasons.append(f"the news brake is {state}")
    return MacroState(bool(reasons), reasons, d1, d5)


__all__ = ["Regime", "RegimeReading", "detect_market_regime", "efficiency_ratio",
           "REGIME_WEIGHTS", "weigh_readings", "direction", "adtv", "adtv_pct",
           "LiquidityCap", "liquidity_cap", "MacroState", "macro_risk_off"]

"""The four-factor strategy: Trend + Momentum + Volume + Volatility, together.

An alternative to the fixed allocation in policy.py, chosen in Settings
(EGX_FOUR_FACTOR). It decides which names to hold and how much of each; the
rest of the pipeline is unchanged -- drift bands, turnover cap, T+2 cash, the
news brake, adopted lab rules, the guard, and the operator pressing Buy.

For each name, from prices up to the previous session only:

- **enter** when all four agree: close above its N-day average (trend), a
  positive N-day return (momentum), 5-day volume at least X% of its N-day
  average (volume), daily volatility at most Y% (volatility);
- **hold** while trend and momentum both still hold. Volume and volatility
  only gate new entries, so a quiet or choppy day does not force a sale;
- **exit** when trend or momentum breaks.

Size: the names held or entered share `invested` of the portfolio in inverse
proportion to their volatility (the calmer name gets more), each capped at
`max_name_weight`. Whatever the cap leaves over stays in cash; it is not
spread onto the other names.

When a name's price history is missing, its target is its current weight: no
trade either way. A data outage must never read as "sell everything".

Every parameter comes from a small sanctioned set (ADMISSIBLE), like the
policy's (docs/NO_OVERFIT_CHARTER.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from ..types import Instrument
from .filters import BuyFilter

ADMISSIBLE: dict[str, tuple] = {
    "trend_days": (50, 100, 200),
    "momentum_days": (20, 60, 120),
    "volume_days": (20,),
    "volume_ratio": (80, 100, 120),
    "vol_days": (20,),
    "max_vol_pct": (2, 3, 4),
    "max_name_weight": (Decimal("0.15"), Decimal("0.20"), Decimal("0.25")),
    "invested": (Decimal("0.80"), Decimal("0.90")),
}


@dataclass(frozen=True)
class FourFactorParams:
    trend_days: int = 100
    momentum_days: int = 20
    volume_days: int = 20
    volume_ratio: int = 100
    vol_days: int = 20
    max_vol_pct: int = 3
    max_name_weight: Decimal = Decimal("0.20")
    invested: Decimal = Decimal("0.90")

    def __post_init__(self) -> None:
        for name, allowed in ADMISSIBLE.items():
            if getattr(self, name) not in allowed:
                raise ValueError(f"{name}={getattr(self, name)} is not a sanctioned value; "
                                 f"allowed: {[str(a) for a in allowed]}")

    @property
    def entry_rule(self) -> BuyFilter:
        return BuyFilter("confluence", {
            "trend_days": self.trend_days, "momentum_days": self.momentum_days,
            "volume_days": self.volume_days, "volume_ratio": self.volume_ratio,
            "vol_days": self.vol_days, "max_vol_pct": self.max_vol_pct,
        })

    @property
    def lookback(self) -> int:
        return self.entry_rule.lookback


@dataclass(frozen=True)
class FourFactorDecision:
    targets: dict[str, Decimal]
    #: symbol -> "enter" | "hold" | "exit" | "out" | "no data"
    actions: dict[str, str]
    reasons: dict[str, str]

    def describe(self) -> str:
        held = [s.removesuffix(".CA") for s, a in self.actions.items() if a in ("enter", "hold")]
        cash = Decimal(1) - sum(self.targets.values())
        return (f"FOUR-FACTOR: {len(held)} of {len(self.actions)} names in"
                + (f" ({', '.join(held)})" if held else "") + f"; cash {cash:.0%}")


def _daily_volatility(closes: Sequence[Decimal], days: int) -> float:
    window = [float(c) for c in closes[-(days + 1):]]
    returns = [(b / a - 1) * 100 for a, b in zip(window, window[1:], strict=False) if a]
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    return (sum((r - mean) ** 2 for r in returns) / len(returns)) ** 0.5


def _trend_and_momentum(closes: Sequence[Decimal], params: FourFactorParams) -> Optional[str]:
    """Why a held name should go; None while trend and momentum both hold."""
    prices = [float(c) for c in closes]
    last = prices[-1]
    average = sum(prices[-params.trend_days:]) / params.trend_days
    start = prices[-(params.momentum_days + 1)]
    objections = []
    if last <= average:
        objections.append(f"trend: close {last:.2f} not above {params.trend_days}-day average "
                          f"{average:.2f}")
    if start and last / start - 1 <= 0:
        objections.append(f"momentum: {(last / start - 1) * 100:+.1f}% over "
                          f"{params.momentum_days} days")
    return "; ".join(objections) or None


def decide(
    universe: Sequence[Instrument],
    closes: Mapping[str, Sequence[Decimal]],
    volumes: Mapping[str, Sequence[Decimal]],
    current_weights: Mapping[str, Decimal],
    params: FourFactorParams = FourFactorParams(),  # noqa: B008 - frozen, validated once
    held_threshold: Decimal = Decimal("0.005"),
) -> FourFactorDecision:
    """Target weights for every name in the universe, from data up to yesterday."""
    actions: dict[str, str] = {}
    reasons: dict[str, str] = {}
    targets: dict[str, Decimal] = {}
    volatility: dict[str, float] = {}
    rule = params.entry_rule

    for instrument in universe:
        symbol = instrument.symbol
        series = closes.get(symbol, ())
        held = current_weights.get(symbol, Decimal(0)) >= held_threshold
        if len(series) < params.lookback:
            actions[symbol] = "no data"
            reasons[symbol] = f"only {len(series)} of {params.lookback} days of prices"
            targets[symbol] = current_weights.get(symbol, Decimal(0))
            continue
        if held:
            why = _trend_and_momentum(series, params)
            actions[symbol] = "exit" if why else "hold"
        else:
            why = rule.blocks(series, volumes.get(symbol, ()))
            actions[symbol] = "out" if why else "enter"
        reasons[symbol] = why or (
            "all four agree" if actions[symbol] == "enter" else "trend and momentum hold")
        if actions[symbol] in ("enter", "hold"):
            volatility[symbol] = max(_daily_volatility(series, params.vol_days), 0.1)
        else:
            targets[symbol] = Decimal(0)

    # Whatever "no data" names keep comes off what the rest may use.
    kept = sum((t for s, t in targets.items() if actions[s] == "no data"), Decimal(0))
    room = max(params.invested - kept, Decimal(0))
    inverse = {s: 1 / v for s, v in volatility.items()}
    total = sum(inverse.values())
    for symbol, weight in inverse.items():
        share = Decimal(str(weight / total)) * room if total else Decimal(0)
        targets[symbol] = min(share, params.max_name_weight).quantize(Decimal("0.0001"))
    return FourFactorDecision(targets=targets, actions=actions, reasons=reasons)

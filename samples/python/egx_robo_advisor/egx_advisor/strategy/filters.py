"""Indicator rules that may only skip buys, tested in the lab before they are used.

An indicator from a trading site enters this bot one way: as a reason not to
buy something today. It cannot create an order, enlarge one, or touch a sell.
That keeps it inside the same discipline as the news filter -- a filter, not a
signal -- and it is what lets a rule be adopted from a lab result without
turning the bot into a chart-pattern trader.

Every rule sees closes up to the previous session only. The lab and the live
agent both call `apply_buy_filters`, so what was tested is what runs.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ..types import ProposedOrder, Side

#: kind -> (label, parameter names with defaults and bounds)
RULE_KINDS: dict[str, dict] = {
    # All four together: trend, momentum, volume and volatility. A buy goes
    # ahead only when every one of them agrees; any one objecting skips it.
    "confluence": {
        "label": "Buy only when trend, momentum, volume and volatility all agree",
        "params": {
            "trend_days": (100, 20, 250),      # close above its N-day average
            "momentum_days": (20, 5, 120),     # positive return over N days
            "volume_days": (20, 10, 60),       # 5-day volume vs N-day average ...
            "volume_ratio": (100, 50, 300),    # ... at least this % of it
            "vol_days": (20, 10, 60),          # daily volatility over N days ...
            "max_vol_pct": (3, 1, 10),         # ... at most this % a day
        },
    },
    "sma": {
        "label": "Skip buys below the N-day moving average",
        "params": {"days": (200, 5, 400)},
    },
    "rsi": {
        "label": "Skip buys when RSI(N) is above a level (overbought)",
        "params": {"days": (14, 2, 100), "above": (70, 50, 95)},
    },
    "momentum": {
        "label": "Skip buys after an N-day fall of more than X%",
        "params": {"days": (20, 2, 250), "fall_pct": (15, 1, 80)},
    },
}


class RuleError(ValueError):
    """A rule's kind or parameters are not allowed."""


@dataclass(frozen=True)
class BuyFilter:
    kind: str
    params: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        spec = RULE_KINDS.get(self.kind)
        if spec is None:
            raise RuleError(f"unknown rule {self.kind!r}; choose from {sorted(RULE_KINDS)}")
        for name, value in self.params.items():
            if name not in spec["params"]:
                raise RuleError(f"{self.kind} has no parameter {name!r}")
            _, low, high = spec["params"][name]
            if not low <= float(value) <= high:
                raise RuleError(f"{self.kind}.{name} must be between {low} and {high}")

    def param(self, name: str) -> float:
        return float(self.params.get(name, RULE_KINDS[self.kind]["params"][name][0]))

    @property
    def lookback(self) -> int:
        """Closes the rule needs before it can say anything."""
        if self.kind == "confluence":
            return max(int(self.param("trend_days")), int(self.param("momentum_days")) + 1,
                       int(self.param("volume_days")), int(self.param("vol_days")) + 1) + 1
        return int(self.param("days")) + 1

    def describe(self, lang: str = "en") -> str:
        from ..i18n import tr

        if self.kind == "confluence":
            return tr("rule.confluence", lang=lang,
                      **{name: f"{self.param(name):.0f}" for name in RULE_KINDS["confluence"]
                         ["params"]})
        return tr(f"rule.{self.kind}", lang=lang, days=f"{self.param('days'):.0f}",
                  above=f"{self.param('above'):.0f}" if self.kind == "rsi" else "",
                  fall=f"{self.param('fall_pct'):.0f}" if self.kind == "momentum" else "")

    def blocks(self, closes: Sequence[Decimal],
               volumes: Optional[Sequence[Decimal]] = None) -> Optional[str]:
        """A reason to skip buying, from closes (and volumes) oldest-first up to yesterday.

        Too little history is itself a reason: a rule that cannot evaluate
        errs towards not buying, like every other gate here.
        """
        if len(closes) < self.lookback:
            return f"{self.describe()}: only {len(closes)} of {self.lookback} days of prices"
        if self.kind == "confluence":
            return self._confluence(closes, volumes or ())
        days = int(self.param("days"))
        last = float(closes[-1])
        if self.kind == "sma":
            average = sum(float(c) for c in closes[-days:]) / days
            if last < average:
                return f"close {last:.2f} below {days}-day average {average:.2f}"
        elif self.kind == "rsi":
            value = _rsi([float(c) for c in closes[-(days + 1):]])
            if value > self.param("above"):
                return f"RSI({days}) {value:.0f} above {self.param('above'):.0f}"
        elif self.kind == "momentum":
            start = float(closes[-(days + 1)])
            change = (last / start - 1) * 100 if start else 0.0
            if change < -self.param("fall_pct"):
                return f"fell {-change:.0f}% over {days} days"
        return None


    def _confluence(self, closes: Sequence[Decimal], volumes: Sequence[Decimal]) -> Optional[str]:
        """Every factor that objects, so the reason says which ones did."""
        prices = [float(c) for c in closes]
        last = prices[-1]
        objections = []

        trend_days = int(self.param("trend_days"))
        average = sum(prices[-trend_days:]) / trend_days
        if last <= average:
            objections.append(f"trend: close {last:.2f} not above {trend_days}-day average "
                              f"{average:.2f}")

        momentum_days = int(self.param("momentum_days"))
        start = prices[-(momentum_days + 1)]
        change = (last / start - 1) * 100 if start else 0.0
        if change <= 0:
            objections.append(f"momentum: {change:+.1f}% over {momentum_days} days")

        volume_days = int(self.param("volume_days"))
        recent = [float(v) for v in volumes[-volume_days:]]
        if len(recent) < volume_days or sum(recent) <= 0:
            objections.append("volume: no volume data")
        else:
            ratio = (sum(recent[-RECENT_VOLUME_DAYS:]) / RECENT_VOLUME_DAYS) / (
                sum(recent) / volume_days) * 100
            if ratio < self.param("volume_ratio"):
                objections.append(f"volume: 5-day volume {ratio:.0f}% of the {volume_days}-day "
                                  f"average, below {self.param('volume_ratio'):.0f}%")

        vol_days = int(self.param("vol_days"))
        window = prices[-(vol_days + 1):]
        returns = [(b / a - 1) * 100 for a, b in zip(window, window[1:], strict=False) if a]
        mean = sum(returns) / len(returns)
        volatility = (sum((r - mean) ** 2 for r in returns) / len(returns)) ** 0.5
        if volatility > self.param("max_vol_pct"):
            objections.append(f"volatility: {volatility:.1f}% a day over {vol_days} days, above "
                              f"{self.param('max_vol_pct'):.0f}%")
        return "; ".join(objections) or None


#: The "recent" side of the volume check: the last trading week.
RECENT_VOLUME_DAYS = 5


def _rsi(closes: Sequence[float]) -> float:
    gains = losses = 0.0
    for previous, current in zip(closes, closes[1:], strict=False):
        move = current - previous
        if move > 0:
            gains += move
        else:
            losses -= move
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100 - 100 / (1 + gains / losses)


def apply_buy_filters(
    orders: Sequence[ProposedOrder],
    filters: Sequence[BuyFilter],
    closes: Mapping[str, Sequence[Decimal]],
    volumes: Optional[Mapping[str, Sequence[Decimal]]] = None,
) -> tuple[list[ProposedOrder], list[tuple[ProposedOrder, str]]]:
    """Drop buys any filter objects to. Sells and everything else pass untouched."""
    kept: list[ProposedOrder] = []
    dropped: list[tuple[ProposedOrder, str]] = []
    for order in orders:
        reason = None
        if order.side is Side.BUY:
            for rule in filters:
                reason = rule.blocks(closes.get(order.symbol, ()),
                                     (volumes or {}).get(order.symbol, ()))
                if reason:
                    break
        if reason:
            dropped.append((order, reason))
        else:
            kept.append(order)
    # The invariant, checked rather than assumed (and not with `assert`, which
    # `python -O` removes): the output is a subset of the input, and only buys
    # were removed.
    if not all(o in orders for o in kept) or any(o.side is not Side.BUY for o, _ in dropped):
        raise RuntimeError("buy filter produced an order it was not given, or dropped a sell")
    return kept, dropped


# --------------------------------------------------------------------------- #
# Adopted rules: config/rules.toml
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AdoptedRule:
    rule: BuyFilter
    adopted_on: date
    #: What the lab measured when it was adopted, kept for the record.
    evidence: str


def load_rules(path: Path) -> tuple[AdoptedRule, ...]:
    if not path.exists():
        return ()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    rules = []
    for entry in raw.get("rule", []):
        params = {k: v for k, v in entry.items() if k in RULE_KINDS.get(entry.get("kind"), {})
                  .get("params", {})}
        rules.append(AdoptedRule(
            rule=BuyFilter(str(entry.get("kind")), params),
            adopted_on=date.fromisoformat(str(entry.get("adopted_on"))),
            evidence=str(entry.get("evidence", "")),
        ))
    return tuple(rules)


def save_rules(path: Path, rules: Sequence[AdoptedRule]) -> None:
    lines = [
        "# Buy filters adopted from the Strategy Lab. Each can only skip buys.",
        "# Adopted only when the lab showed it beating the plain policy after costs,",
        "# in both halves of the history. Remove a rule from the app or here.",
        "",
    ]
    for adopted in rules:
        evidence = adopted.evidence.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        lines += ["[[rule]]", f'kind = "{adopted.rule.kind}"']
        lines += [f"{k} = {float(v):g}" for k, v in sorted(adopted.rule.params.items())]
        lines += [f"adopted_on = {adopted.adopted_on.isoformat()}",
                  f'evidence = "{evidence}"', ""]
    path.write_text("\n".join(lines), encoding="utf-8")

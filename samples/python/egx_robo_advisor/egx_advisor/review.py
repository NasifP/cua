"""How did the picks do? Scan picks and plan orders, measured afterwards.

For every pick in memory (memory.py), the close 5, 20 and 60 sessions after
the pick is compared with the price at the pick. A buy did well when the price
rose, a sell when it fell. A horizon not reached yet is left blank, never
guessed.

The scorecard adds them up per source ("scan" and "plan"): how many picks
have reached each horizon, their average move and how many went the right
way. It measures what happened; it is not a claim about what will.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Mapping, Optional, Sequence

from .memory import Pick

HORIZONS = (5, 20, 60)


@dataclass(frozen=True)
class PickResult:
    pick: Pick
    #: horizon -> signed return in % (positive = the pick went the right way)
    moves: Mapping[int, Optional[float]]


@dataclass(frozen=True)
class Score:
    source: str
    horizon: int
    count: int
    average: float
    right: int

    @property
    def hit_rate(self) -> float:
        return self.right / self.count if self.count else 0.0


def measure(pick: Pick, closes: Sequence[tuple[date, float]]) -> PickResult:
    """Closes are (day, close), oldest first. Sessions count from after the pick day."""
    after = [c for d, c in closes if d > pick.day]
    moves: dict[int, Optional[float]] = {}
    for horizon in HORIZONS:
        if len(after) >= horizon and pick.price > 0:
            change = (after[horizon - 1] / pick.price - 1) * 100
            moves[horizon] = change if pick.side == "buy" else -change
        else:
            moves[horizon] = None
    return PickResult(pick, moves)


def review(picks: Sequence[Pick],
           closes_for: Callable[[str], Sequence[tuple[date, float]]]) -> list[PickResult]:
    cache: dict[str, Sequence[tuple[date, float]]] = {}
    results = []
    for pick in picks:
        if pick.symbol not in cache:
            cache[pick.symbol] = closes_for(pick.symbol)
        results.append(measure(pick, cache[pick.symbol]))
    return results


def scorecard(results: Sequence[PickResult]) -> list[Score]:
    scores = []
    for source in sorted({r.pick.source for r in results}):
        for horizon in HORIZONS:
            moves = [r.moves[horizon] for r in results
                     if r.pick.source == source and r.moves[horizon] is not None]
            if moves:
                scores.append(Score(source, horizon, len(moves), sum(moves) / len(moves),
                                    sum(1 for m in moves if m > 0)))
    return scores


def describe(scores: Sequence[Score], arabic: bool) -> str:
    """The scorecard as text, for the Memory tab and the chat."""
    if not scores:
        return ("لسه مافيش اختيارات عدّى عليها 5 جلسات. المراجعة بتبدأ بعد كده."
                if arabic else "No pick is 5 sessions old yet; the review starts then.")
    names = {"scan": ("البحث عن الفرص", "opportunity scan"), "plan": ("خطة البوت", "bot's plan")}
    lines = []
    for s in scores:
        name = names.get(s.source, (s.source, s.source))[0 if arabic else 1]
        if arabic:
            lines.append(f"{name} بعد {s.horizon} جلسة: {s.count} اختيار، متوسط الحركة "
                         f"{s.average:+.1f}%، صح في {s.right} من {s.count} ({s.hit_rate:.0%})")
        else:
            lines.append(f"{name} after {s.horizon} sessions: {s.count} picks, average "
                         f"{s.average:+.1f}%, right {s.right} of {s.count} ({s.hit_rate:.0%})")
    lines.append("قبل العمولة. ده قياس للي حصل، مش توقع للي جاي." if arabic else
                 "Before fees. A measure of what happened, not a forecast.")
    return "\n".join(lines)

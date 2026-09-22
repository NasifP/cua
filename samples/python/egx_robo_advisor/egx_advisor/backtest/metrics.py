"""Metrics, and an explicit guard against reading noise as signal.

The headline number a backtest produces is almost always the least trustworthy
thing in it. Two configurations differing by 40bps of annual return over a
three-year sample is not evidence that one is better -- it is one sample path,
and the difference is usually well inside the range you would get by reshuffling
the same returns.

So every comparison here carries a **stationary block bootstrap** confidence
interval on the difference, and `Comparison.conclusive` is False whenever that
interval straddles zero. The report prints "indistinguishable from noise" in
that case, in as many words. This is the numerical counterpart to
`PolicyParameters.validate()`: the charter forbids fitting parameters, and this
makes it visible when a fitted difference would have been meaningless anyway.

Block bootstrap rather than plain resampling because daily returns are
autocorrelated and volatility clusters; sampling individual days independently
would destroy that structure and produce intervals that are far too narrow.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from .types import BacktestResult

TRADING_DAYS_PER_YEAR = 245  # EGX runs Sun-Thu, ~49 weeks


@dataclass(frozen=True, slots=True)
class Metrics:
    label: str
    starting_value: Decimal
    ending_value: Decimal
    total_return: float
    cagr: float
    volatility: float
    max_drawdown: float
    #: Costs as a fraction of the starting value.
    cost_drag: float
    total_costs: Decimal
    total_turnover: Decimal
    annual_turnover_ratio: float
    fills: int
    rejections: int
    fill_rate: float
    #: Daily sum of |actual - target| across the universe, averaged over the run.
    #: It is a SUM over names, so ~14% across seven names is ~2% each, not 14% each.
    mean_drift: float
    days: int

    def render(self) -> str:
        return "\n".join([
            f"  label                 {self.label}",
            f"  period                {self.days} sessions",
            f"  value                 {self.starting_value:,.0f} -> {self.ending_value:,.0f} EGP",
            f"  total return          {self.total_return:+.2%}",
            f"  CAGR                  {self.cagr:+.2%}",
            f"  volatility (annual)   {self.volatility:.2%}",
            f"  max drawdown          {self.max_drawdown:.2%}",
            f"  costs paid            {self.total_costs:,.0f} EGP  ({self.cost_drag:.2%} of start)",
            f"  turnover              {self.total_turnover:,.0f} EGP  "
            f"({self.annual_turnover_ratio:.2f}x/yr)",
            f"  fills / rejections    {self.fills} / {self.rejections}  "
            f"(fill rate {self.fill_rate:.0%})",
            f"  drift from target     {self.mean_drift:.2%}  "
            f"(summed across names, daily mean)",
        ])


def compute(result: BacktestResult) -> Metrics:
    snapshots = result.snapshots
    if not snapshots:
        raise ValueError("no snapshots to measure")

    start = result.starting_value or snapshots[0].total_value
    end = snapshots[-1].total_value
    total_return = float((end - start) / start) if start else 0.0

    returns = result.daily_returns()
    years = max(len(snapshots) / TRADING_DAYS_PER_YEAR, 1e-9)
    cagr = ((1 + total_return) ** (1 / years) - 1) if total_return > -1 else -1.0
    vol = (
        statistics.stdev(returns) * math.sqrt(TRADING_DAYS_PER_YEAR)
        if len(returns) > 1
        else 0.0
    )

    peak = float(snapshots[0].total_value)
    max_dd = 0.0
    for snapshot in snapshots:
        value = float(snapshot.total_value)
        peak = max(peak, value)
        if peak > 0:
            max_dd = min(max_dd, value / peak - 1)

    costs = result.total_costs
    turnover = result.total_turnover
    attempted = len(result.fills) + len(result.rejections)

    return Metrics(
        label=result.label,
        starting_value=start,
        ending_value=end,
        total_return=total_return,
        cagr=cagr,
        volatility=vol,
        max_drawdown=max_dd,
        cost_drag=float(costs / start) if start else 0.0,
        total_costs=costs,
        total_turnover=turnover,
        annual_turnover_ratio=(float(turnover / start) / years) if start else 0.0,
        fills=len(result.fills),
        rejections=len(result.rejections),
        fill_rate=(len(result.fills) / attempted) if attempted else 1.0,
        mean_drift=(
            statistics.fmean(float(s.total_drift) for s in snapshots) if snapshots else 0.0
        ),
        days=len(snapshots),
    )


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Comparison:
    left: str
    right: str
    #: Mean daily return difference, annualised.
    difference: float
    ci_low: float
    ci_high: float
    samples: int

    @property
    def conclusive(self) -> bool:
        """True only when the interval excludes zero."""
        return (self.ci_low > 0) or (self.ci_high < 0)

    def render(self) -> str:
        verdict = (
            f"{self.left} beats {self.right}"
            if self.conclusive and self.difference > 0
            else f"{self.right} beats {self.left}"
            if self.conclusive
            else "INDISTINGUISHABLE FROM NOISE"
        )
        return "\n".join([
            f"  {self.left}  vs  {self.right}",
            f"    annualised difference   {self.difference:+.2%}",
            f"    95% bootstrap interval  [{self.ci_low:+.2%}, {self.ci_high:+.2%}]",
            f"    verdict                 {verdict}",
        ])


def compare(
    left: BacktestResult,
    right: BacktestResult,
    *,
    iterations: int = 2000,
    block_size: Optional[int] = None,
    seed: int = 20260922,
) -> Comparison:
    """Block-bootstrap the difference in daily returns between two runs.

    The interval answers the only question worth asking of a backtest
    comparison: could this difference plausibly be zero?
    """
    a = left.daily_returns()
    b = right.daily_returns()
    n = min(len(a), len(b))
    if n < 30:
        # Too short to say anything. Report an interval so wide it is obviously
        # uninformative rather than returning a confident-looking number.
        diff = _annualise(_mean(a[:n]) - _mean(b[:n]))
        return Comparison(left.label, right.label, diff, -abs(diff) * 10 - 1, abs(diff) * 10 + 1, n)

    a, b = a[:n], b[:n]
    paired = [x - y for x, y in zip(a, b, strict=True)]
    block = block_size or max(5, int(round(n ** (1 / 3))))
    rng = random.Random(seed)

    means: list[float] = []
    blocks_needed = math.ceil(n / block)
    for _ in range(iterations):
        sample: list[float] = []
        for _ in range(blocks_needed):
            start = rng.randrange(n)
            # Circular blocks keep every observation equally likely to be drawn.
            sample.extend(paired[(start + i) % n] for i in range(block))
        means.append(_mean(sample[:n]))

    means.sort()
    low = means[int(0.025 * len(means))]
    high = means[min(len(means) - 1, int(0.975 * len(means)))]
    return Comparison(
        left=left.label,
        right=right.label,
        difference=_annualise(_mean(paired)),
        ci_low=_annualise(low),
        ci_high=_annualise(high),
        samples=n,
    )


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _annualise(daily: float) -> float:
    return daily * TRADING_DAYS_PER_YEAR


def sample_size_warning(days: int) -> Optional[str]:
    """Flag a sample too small to support any parameter conclusion.

    EGX's usable history contains a handful of independent macro episodes, not
    hundreds. Three years of daily prints is three years of one path, and the
    charter's rule 8 exists because that path is extremely easy to fit.
    """
    years = days / TRADING_DAYS_PER_YEAR
    if years < 3:
        return (
            f"Sample is {years:.1f} years ({days} sessions). Far too short to support any "
            f"conclusion about parameter values. Treat cost and turnover figures as "
            f"indicative and return figures as anecdote."
        )
    if years < 8:
        return (
            f"Sample is {years:.1f} years. Enough to compare frictions, not enough to "
            f"choose parameters -- it likely contains only one or two independent macro "
            f"episodes. See docs/NO_OVERFIT_CHARTER.md rule 8."
        )
    return None

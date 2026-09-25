"""The Strategy Lab: does an indicator rule from a trading site help, after costs?

A rule is tested the one way it could ever be used here -- as a filter that
skips buys -- against the same policy without it, on the same history, with the
same fills and costs. It passes only if all three hold:

1. over the whole history it beats the plain policy, and the block-bootstrap
   interval excludes zero (not "INDISTINGUISHABLE FROM NOISE");
2. it is ahead in the first half of the history, run on its own;
3. it is ahead in the second half, run on its own.

The halves are the guard against a rule that only worked in one stretch of
the market. The parameters come from the operator or the site, not from
searching here, which is why the lab runs one rule set at a time and never
ranks parameter values (docs/NO_OVERFIT_CHARTER.md). A failed rule is a result,
and the common one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

from ..i18n import tr
from ..strategy.filters import BuyFilter
from .engine import BacktestConfig, run_backtest, run_buy_and_hold
from .metrics import Comparison, Metrics, compare, compute, sample_size_warning
from .types import PriceHistory

MIN_SESSIONS = 120


@dataclass(frozen=True)
class LabReport:
    rules: tuple[BuyFilter, ...]
    with_rules: Metrics
    policy: Metrics
    buy_and_hold: Metrics
    full: Comparison
    first_half: Comparison
    second_half: Comparison
    sessions: int
    warning: Optional[str]
    synthetic: bool = False

    @property
    def passed(self) -> bool:
        return (
            not self.synthetic
            and self.full.conclusive
            and self.full.difference > 0
            and self.first_half.difference > 0
            and self.second_half.difference > 0
        )

    def verdict(self, lang: str = "en") -> str:
        if self.synthetic:
            key = "verdict.synthetic"
        elif self.passed:
            key = "verdict.passed"
        elif not self.full.conclusive:
            key = "verdict.noise"
        elif self.full.difference <= 0:
            key = "verdict.hurts"
        else:
            key = "verdict.inconsistent"
        return tr(key, lang=lang)

    def evidence(self) -> str:
        """One line kept with an adopted rule."""
        return (
            f"{self.sessions} sessions: {self.full.difference:+.2%}/yr vs policy "
            f"[{self.full.ci_low:+.2%}, {self.full.ci_high:+.2%}], halves "
            f"{self.first_half.difference:+.2%} / {self.second_half.difference:+.2%}"
        )

    def render(self, lang: str = "en") -> str:
        def t(key: str) -> str:
            return tr(key, lang=lang)

        rows = [
            ("", t("report.with_rules"), t("report.policy"), t("report.hold")),
            (t("report.total_return"), *(f"{m.total_return:+.1%}" for m in self._all())),
            (t("report.max_drawdown"), *(f"{m.max_drawdown:.1%}" for m in self._all())),
            (t("report.costs"), *(f"{m.total_costs:,.0f}" for m in self._all())),
            (t("report.fills"), *(str(m.fills) for m in self._all())),
        ]
        table = "\n".join(f"  {a:<14}{b:>14}{c:>14}{d:>14}" for a, b, c, d in rows)
        parts = [
            t("report.rules") + ": " + "; ".join(r.describe(lang) for r in self.rules),
            table,
            "",
            t("report.versus"),
            f"  {t('report.whole'):<14} {self.full.difference:+.2%}  "
            f"{t('report.interval')} [{self.full.ci_low:+.2%}, {self.full.ci_high:+.2%}]",
            f"  {t('report.first'):<14} {self.first_half.difference:+.2%}",
            f"  {t('report.second'):<14} {self.second_half.difference:+.2%}",
            "",
            t("report.verdict") + ": " + self.verdict(lang),
        ]
        if self.warning:
            parts += ["", t("report.note") + ": " + self.warning]
        return "\n".join(parts)

    def _all(self) -> tuple[Metrics, Metrics, Metrics]:
        return self.with_rules, self.policy, self.buy_and_hold


def _halves(history: PriceHistory) -> tuple[PriceHistory, PriceHistory]:
    days = list(history.days)
    mid = len(days) // 2

    def part(selected: Sequence) -> PriceHistory:
        keep = set(selected)
        return PriceHistory(
            bars={d: b for d, b in history.bars.items() if d in keep},
            macro=history.macro,
            days=tuple(selected),
        )

    return part(days[:mid]), part(days[mid:])


def _pair(history: PriceHistory, config: BacktestConfig,
          rules: Sequence[BuyFilter]) -> Comparison:
    base = run_backtest(history, replace(config, buy_filters=(), label="policy"))
    ruled = run_backtest(history, replace(config, buy_filters=tuple(rules), label="with rules"))
    return compare(ruled, base)


def run_lab(
    history: PriceHistory,
    rules: Sequence[BuyFilter],
    config: Optional[BacktestConfig] = None,
    *,
    synthetic: bool = False,
) -> LabReport:
    if not rules:
        raise ValueError("choose at least one rule to test")
    sessions = len(history.days)
    if sessions < MIN_SESSIONS:
        raise ValueError(f"only {sessions} sessions of history; the lab needs {MIN_SESSIONS}+")
    config = config or BacktestConfig()
    base = run_backtest(history, replace(config, buy_filters=(), label="policy"))
    ruled = run_backtest(history, replace(config, buy_filters=tuple(rules), label="with rules"))
    hold = run_buy_and_hold(history, replace(config, label="buy & hold"))
    first, second = _halves(history)
    return LabReport(
        rules=tuple(rules),
        with_rules=compute(ruled),
        policy=compute(base),
        buy_and_hold=compute(hold),
        full=compare(ruled, base),
        first_half=_pair(first, config, rules),
        second_half=_pair(second, config, rules),
        sessions=sessions,
        warning=sample_size_warning(sessions),
        synthetic=synthetic,
    )


def adopt(report: LabReport, path: Path, today: date) -> None:
    """Add a passed report's rules to config/rules.toml. Refuses anything else.

    The check is here, not only in the button that calls it, so no other caller
    can adopt a rule the lab did not pass.
    """
    from ..strategy.filters import AdoptedRule, load_rules, save_rules

    if not report.passed:
        raise ValueError(f"not adopted: {report.verdict()}")
    existing = [a for a in load_rules(path) if a.rule not in report.rules]
    added = [AdoptedRule(rule, today, report.evidence()) for rule in report.rules]
    save_rules(path, existing + added)


def remove(rule: BuyFilter, path: Path) -> None:
    from ..strategy.filters import load_rules, save_rules

    save_rules(path, [a for a in load_rules(path) if a.rule != rule])

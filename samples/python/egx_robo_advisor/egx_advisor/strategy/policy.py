"""Allocation policy for the EGX. Structural, discrete, and deliberately dumb.

The design position
-------------------
An EGX rebalancer has a small number of real edges and a very large number of
ways to fool itself. The market is concentrated, the currency has repriced hard
several times in a decade, and the usable price history contains only a handful
of genuinely independent macro episodes. Any model with more than a couple of
free parameters will fit those episodes rather than learn from them.

So the policy here is:

  * **equal weight inside each sleeve.** No optimiser, no covariance estimate, no
    expected returns. Equal weighting has one parameter -- the membership list --
    and it is the hardest allocation rule in existence to overfit.
  * **two macro states, one threshold.** Either trailing EGP depreciation has
    breached the devaluation trigger or it has not. There is no continuum to tune,
    no interpolation, no "risk score".
  * **drift bands, not schedules.** We trade when the portfolio has actually
    drifted, which is when rebalancing is worth its cost, not on a calendar.
  * **parameters drawn from a sanctioned discrete set**, enforced at runtime by
    `PolicyParameters.validate()`. A parameter of 0.0437 is a fitted parameter;
    the constructor rejects it. This is the one piece of overfitting protection
    that survives contact with a motivated operator at 1am, because it is a
    raised exception rather than a paragraph in a README.

See docs/NO_OVERFIT_CHARTER.md for the rules governing changes to any of this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Sequence

from ..types import Instrument, Sleeve

# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #

#: Large, liquid EGX names plus the gold sleeve. This is *policy data*, not a
#: screen result: membership changes are a deliberate decision, made rarely, and
#: recorded in the charter. Verify every symbol against the live EGX listing
#: before arming the bot -- tickers get renamed, merged, and suspended, and the
#: gold ETF in particular should be confirmed against its current listing code.
DEFAULT_UNIVERSE: tuple[Instrument, ...] = (
    Instrument("COMI.CA", "Commercial International Bank", Sleeve.BLUE_CHIP),
    Instrument("TMGH.CA", "Talaat Moustafa Group Holding", Sleeve.BLUE_CHIP),
    Instrument("SWDY.CA", "Elsewedy Electric", Sleeve.BLUE_CHIP),
    Instrument("ABUK.CA", "Abu Qir Fertilizers", Sleeve.BLUE_CHIP),
    Instrument("EAST.CA", "Eastern Company", Sleeve.BLUE_CHIP),
    Instrument("ETEL.CA", "Telecom Egypt", Sleeve.BLUE_CHIP),
    Instrument("AZG.CA", "Gold-tracking ETF (confirm listing code)", Sleeve.INFLATION_HEDGE),
)

#: Sanctioned values. Anything outside these sets is treated as a fitted
#: parameter and rejected. Widening a set is a charter-level change.
ADMISSIBLE = {
    "sleeve_drift_band": (Decimal("0.03"), Decimal("0.05"), Decimal("0.08")),
    "name_drift_band": (Decimal("0.02"), Decimal("0.03"), Decimal("0.05")),
    "devaluation_trigger": (Decimal("0.10"), Decimal("0.15"), Decimal("0.20")),
    "max_name_weight": (Decimal("0.10"), Decimal("0.15"), Decimal("0.20")),
    "max_turnover_per_cycle": (Decimal("0.10"), Decimal("0.15"), Decimal("0.25")),
}


@dataclass(frozen=True, slots=True)
class SleeveTargets:
    """Weights across the three sleeves. Must sum to 1."""

    blue_chip: Decimal
    inflation_hedge: Decimal
    cash: Decimal

    def __post_init__(self) -> None:
        total = self.blue_chip + self.inflation_hedge + self.cash
        if abs(total - Decimal(1)) > Decimal("0.0001"):
            raise ValueError(f"sleeve targets must sum to 1, got {total}")
        if min(self.blue_chip, self.inflation_hedge, self.cash) < 0:
            raise ValueError("sleeve targets cannot be negative")

    def weight(self, sleeve: Sleeve) -> Decimal:
        return {
            Sleeve.BLUE_CHIP: self.blue_chip,
            Sleeve.INFLATION_HEDGE: self.inflation_hedge,
            Sleeve.CASH: self.cash,
        }[sleeve]


#: Baseline: equity-led, with a standing gold sleeve because EGP inflation is a
#: permanent feature of this market rather than an event to time.
BASELINE_TARGETS = SleeveTargets(
    blue_chip=Decimal("0.60"), inflation_hedge=Decimal("0.30"), cash=Decimal("0.10")
)

#: Devaluation-stress state. The hedge rises and equity falls; cash is unchanged
#: because holding more EGP cash during a devaluation is the one thing that
#: reliably loses. This is the *entire* macro model: two states, one trigger.
DEVALUATION_TARGETS = SleeveTargets(
    blue_chip=Decimal("0.45"), inflation_hedge=Decimal("0.45"), cash=Decimal("0.10")
)


@dataclass(frozen=True, slots=True)
class PolicyParameters:
    """Every tunable in the strategy. Seven numbers, all discrete."""

    #: Sleeve-level no-trade band, absolute weight.
    sleeve_drift_band: Decimal = Decimal("0.05")
    #: Name-level no-trade band, absolute weight.
    name_drift_band: Decimal = Decimal("0.03")
    #: Trailing EGP depreciation that flips us into the devaluation state.
    devaluation_trigger: Decimal = Decimal("0.15")
    #: Lookback for that depreciation measure, in calendar days.
    devaluation_lookback_days: int = 180
    #: Concentration cap per name.
    max_name_weight: Decimal = Decimal("0.15")
    #: Fraction of portfolio value that may turn over in one cycle.
    max_turnover_per_cycle: Decimal = Decimal("0.15")
    #: Commission/spread floor: below this, rebalancing costs more than the drift.
    min_order_notional_egp: Decimal = Decimal("5000")
    #: Session days a name must rest after being traded, to stop oscillation.
    symbol_cooldown_days: int = 5

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Reject parameters outside the sanctioned discrete sets.

        This is the anti-overfitting guardrail with teeth. Tuning a band to
        0.0437 because it backtested well is precisely the failure mode, and the
        only reliable way to prevent it is to make the value unrepresentable.
        """
        for name, allowed in ADMISSIBLE.items():
            value = getattr(self, name)
            if value not in allowed:
                raise ValueError(
                    f"{name}={value} is not a sanctioned value. Allowed: "
                    f"{[str(a) for a in allowed]}. If you need a different value, "
                    f"change the charter and ADMISSIBLE deliberately -- do not fit it."
                )
        if self.devaluation_lookback_days not in (90, 180, 365):
            raise ValueError("devaluation_lookback_days must be one of 90, 180, 365")
        if self.symbol_cooldown_days not in (0, 3, 5, 10):
            raise ValueError("symbol_cooldown_days must be one of 0, 3, 5, 10")
        if self.min_order_notional_egp <= 0:
            raise ValueError("min_order_notional_egp must be positive")


@dataclass(frozen=True, slots=True)
class AllocationPolicy:
    """Universe plus parameters, and the target weights they imply."""

    universe: tuple[Instrument, ...] = DEFAULT_UNIVERSE
    params: PolicyParameters = field(default_factory=PolicyParameters)
    baseline: SleeveTargets = BASELINE_TARGETS
    devaluation: SleeveTargets = DEVALUATION_TARGETS

    def __post_init__(self) -> None:
        symbols = [i.symbol for i in self.universe]
        if len(symbols) != len(set(symbols)):
            raise ValueError("duplicate symbol in universe")
        if not self.by_sleeve(Sleeve.BLUE_CHIP):
            raise ValueError("universe needs at least one blue chip")
        if not self.by_sleeve(Sleeve.INFLATION_HEDGE):
            raise ValueError("universe needs an inflation hedge; that is the point")

    def by_sleeve(self, sleeve: Sleeve) -> tuple[Instrument, ...]:
        return tuple(i for i in self.universe if i.sleeve is sleeve)

    def instrument(self, symbol: str) -> Instrument | None:
        return next((i for i in self.universe if i.symbol == symbol), None)

    def in_devaluation_state(self, egp_depreciation: Decimal) -> bool:
        return egp_depreciation >= self.params.devaluation_trigger

    def sleeve_targets(self, egp_depreciation: Decimal) -> SleeveTargets:
        return (
            self.devaluation
            if self.in_devaluation_state(egp_depreciation)
            else self.baseline
        )

    def target_weights(self, egp_depreciation: Decimal) -> dict[str, Decimal]:
        """Per-symbol target weights: sleeve weight split equally, then capped.

        Equal weighting is the whole allocation rule. Capping can leave the sum
        short of the sleeve target; the residue lands in cash rather than being
        redistributed, because redistributing it would quietly reintroduce
        concentration through the back door.

        `max_name_weight` is a single-*issuer* concentration limit, so it applies
        to the equity sleeve only. Applying it to the hedge sleeve would be a
        category error with a nasty consequence: with one gold ETF in the sleeve,
        a 15% name cap silently clamps a 30% hedge target to 15% and the entire
        devaluation response -- the reason the sleeve exists -- becomes inert.
        A gold ETF carries no single-company idiosyncratic risk for this cap to
        limit, so the hedge sleeve is bounded by its sleeve target instead.
        """
        targets: dict[str, Decimal] = {}
        sleeve_targets = self.sleeve_targets(egp_depreciation)

        for sleeve in (Sleeve.BLUE_CHIP, Sleeve.INFLATION_HEDGE):
            members = self.by_sleeve(sleeve)
            if not members:
                continue
            per_name = sleeve_targets.weight(sleeve) / Decimal(len(members))
            cap = (
                self.params.max_name_weight
                if sleeve is Sleeve.BLUE_CHIP
                else sleeve_targets.weight(sleeve)
            )
            for instrument in members:
                targets[instrument.symbol] = min(per_name, cap)
        return targets

    def describe(self, egp_depreciation: Decimal) -> str:
        state = "DEVALUATION-STRESS" if self.in_devaluation_state(egp_depreciation) else "BASELINE"
        targets = self.sleeve_targets(egp_depreciation)
        return (
            f"{state}: equity {targets.blue_chip:.0%} / hedge "
            f"{targets.inflation_hedge:.0%} / cash {targets.cash:.0%} "
            f"(trailing EGP depreciation {egp_depreciation:.1%}, trigger "
            f"{self.params.devaluation_trigger:.0%})"
        )

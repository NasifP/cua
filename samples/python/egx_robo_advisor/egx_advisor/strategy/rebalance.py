"""Drift-band rebalancing, with EGX's actual frictions encoded.

`plan_rebalance` is a pure function: state in, plan out. No clock, no network, no
screen. That is what makes it backtestable against a replayed history and
unit-testable without a broker, and it is why the whole strategy layer is worth
keeping separate from the thing that clicks buttons.

Frictions this models, because on the EGX they decide whether a plan is real:

  * **Board lots.** Orders round down to a lot multiple, and an order that rounds
    to zero is dropped rather than silently sent as a 1-share order.
  * **Daily price limits.** A name printing at its +/-10% limit is not reliably
    fillable and, more to the point, a rebalancer buying a limit-up name is
    momentum-chasing with extra steps. We stand aside.
  * **Halts and stale prints.** No quote, no order.
  * **T+2 settlement.** Today's sale proceeds are not investable today, so buys
    are sized against settled cash only.
  * **Commission floor.** Below `min_order_notional_egp` the round trip costs more
    than the drift it corrects.
  * **Turnover cap and per-symbol cooldown.** Together these stop the bot
    oscillating around a band edge and grinding the account down in fees.

Sells are planned before buys so that de-risking never waits on cash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Mapping, Optional, Sequence

from ..types import (
    Instrument,
    MarketSnapshot,
    Portfolio,
    ProposedOrder,
    Quote,
    RegimeState,
    Side,
    Sleeve,
    Tradability,
)
from .policy import AllocationPolicy

#: Limit orders are priced this far through the last print, so a rebalance is not
#: hostage to a one-tick move between the screenshot and the tap. It is a slippage
#: allowance, not a view on price.
LIMIT_SLIPPAGE = Decimal("0.005")

#: How close to the daily limit counts as "at the limit" for our purposes.
LIMIT_BAND_PROXIMITY = Decimal("0.9")


@dataclass(frozen=True, slots=True)
class SkippedOrder:
    """An order we decided against, and why. Rendered on the dashboard."""

    symbol: str
    side: Side
    reason: str
    drift: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class RebalancePlan:
    """The full output: what to do, what we declined, and the reasoning."""

    as_of: datetime
    orders: tuple[ProposedOrder, ...] = ()
    skipped: tuple[SkippedOrder, ...] = ()
    target_weights: Mapping[str, Decimal] = field(default_factory=dict)
    current_weights: Mapping[str, Decimal] = field(default_factory=dict)
    policy_state: str = ""
    notes: tuple[str, ...] = ()

    @property
    def turnover_egp(self) -> Decimal:
        return sum((o.notional for o in self.orders), Decimal(0))

    @property
    def is_empty(self) -> bool:
        return not self.orders


def plan_rebalance(
    *,
    portfolio: Portfolio,
    market: MarketSnapshot,
    policy: AllocationPolicy,
    regime: Optional[RegimeState] = None,
    last_traded: Optional[Mapping[str, date]] = None,
    today: Optional[date] = None,
) -> RebalancePlan:
    """Produce the unfiltered rebalancing plan.

    `regime` is accepted for reporting only -- the strategy layer never consults
    it to decide what to buy. Suppression is the regime filter's job, applied to
    this plan afterwards, so that the two concerns stay separable and each stays
    testable. See regime/filter.py for why the ordering matters.
    """
    now = market.as_of
    today = today or now.date()
    last_traded = last_traded or {}
    total = portfolio.total_value

    notes: list[str] = []
    skipped: list[SkippedOrder] = []

    if total <= 0:
        return RebalancePlan(
            as_of=now,
            policy_state=policy.describe(market.egp_depreciation),
            notes=("portfolio has no value; nothing to rebalance",),
        )

    depreciation = market.egp_depreciation
    targets = policy.target_weights(depreciation)
    current = {symbol: portfolio.weight_of(symbol) for symbol in targets}
    policy_state = policy.describe(depreciation)

    # --------------------------------------------------------------- drift pass
    candidates: list[tuple[Decimal, ProposedOrder]] = []

    for symbol, target in targets.items():
        instrument = policy.instrument(symbol)
        assert instrument is not None  # targets are built from the universe
        held = current.get(symbol, Decimal(0))
        drift = target - held
        band = policy.params.name_drift_band

        if abs(drift) < band:
            continue

        quote = market.quote(symbol)
        if quote is None:
            skipped.append(SkippedOrder(symbol, Side.BUY, "no quote available", drift))
            continue

        tradable, reason = _is_tradable(quote, instrument.price_limit_pct, buying=drift > 0)
        if not tradable:
            skipped.append(
                SkippedOrder(symbol, Side.BUY if drift > 0 else Side.SELL, reason, drift)
            )
            continue

        rested, rest_reason = _has_rested(
            symbol, last_traded, today, policy.params.symbol_cooldown_days
        )
        if not rested:
            skipped.append(
                SkippedOrder(symbol, Side.BUY if drift > 0 else Side.SELL, rest_reason, drift)
            )
            continue

        side = Side.BUY if drift > 0 else Side.SELL
        notional = abs(drift) * total
        limit_price = _limit_price(quote, side)
        if limit_price <= 0:
            skipped.append(SkippedOrder(symbol, side, "non-positive limit price", drift))
            continue

        quantity = _round_to_lot(notional / limit_price, instrument.lot_size)
        if quantity <= 0:
            skipped.append(
                SkippedOrder(
                    symbol,
                    side,
                    f"rounds to zero at lot size {instrument.lot_size}",
                    drift,
                )
            )
            continue

        order = ProposedOrder(
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            drift=drift,
            rationale=(
                f"{symbol} at {held:.1%} vs target {target:.1%} "
                f"({drift:+.1%} drift, band {band:.1%})"
            ),
        )

        if order.notional < policy.params.min_order_notional_egp:
            skipped.append(
                SkippedOrder(
                    symbol,
                    side,
                    (
                        f"notional {order.notional:.0f} EGP below "
                        f"{policy.params.min_order_notional_egp:.0f} EGP floor"
                    ),
                    drift,
                )
            )
            continue

        candidates.append((abs(drift), order))

    # Largest drift first: if the turnover cap binds, correct the worst gap.
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    ordered = [order for _, order in candidates]

    # Sells before buys, so de-risking is never blocked waiting on cash.
    sells = [o for o in ordered if o.side is Side.SELL]
    buys = [o for o in ordered if o.side is Side.BUY]

    accepted: list[ProposedOrder] = []
    turnover_budget = policy.params.max_turnover_per_cycle * total
    spent = Decimal(0)

    # A budget-busting order is trimmed to what fits, never dropped. Dropping it
    # is actively perverse: the largest drift is the most important thing to
    # correct, and skipping it in favour of two small orders that happen to fit
    # leaves the portfolio further from target than a partial fill would. Sells
    # go first, so the worst overweight gets first claim on the budget.
    for order in sells:
        fitted, reason = _fit_to_budget(
            order,
            turnover_budget - spent,
            lot_size=(policy.instrument(order.symbol) or _unit).lot_size,
            min_notional=policy.params.min_order_notional_egp,
            constraint="turnover cap",
        )
        if fitted is None:
            skipped.append(SkippedOrder(order.symbol, order.side, reason, order.drift))
            continue
        accepted.append(fitted)
        spent += fitted.notional

    # Buys are constrained by settled cash as well: EGX equities settle T+2, so
    # proceeds from the sells above are NOT available in this cycle.
    cash_budget = portfolio.investable_cash
    if portfolio.unsettled_cash_egp > 0:
        notes.append(
            f"{portfolio.unsettled_cash_egp:.0f} EGP still unsettled (T+2) and "
            f"excluded from buying power"
        )

    for order in buys:
        lot_size = (policy.instrument(order.symbol) or _unit).lot_size
        budget = min(turnover_budget - spent, cash_budget)
        constraint = (
            "turnover cap" if (turnover_budget - spent) <= cash_budget else "settled cash"
        )
        fitted, reason = _fit_to_budget(
            order,
            budget,
            lot_size=lot_size,
            min_notional=policy.params.min_order_notional_egp,
            constraint=constraint,
        )
        if fitted is None:
            skipped.append(SkippedOrder(order.symbol, order.side, reason, order.drift))
            continue
        accepted.append(fitted)
        spent += fitted.notional
        cash_budget -= fitted.notional

    return RebalancePlan(
        as_of=now,
        orders=tuple(accepted),
        skipped=tuple(skipped),
        target_weights=targets,
        current_weights=current,
        policy_state=policy_state,
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

#: Fallback lot size for a symbol that somehow is not in the universe. Reaching
#: this means a bug upstream, so it stays conservative at one share.
_unit = Instrument("XXXX.CA", "fallback", Sleeve.BLUE_CHIP, lot_size=1)


def _is_tradable(
    quote: Quote, price_limit_pct: Decimal, *, buying: bool
) -> tuple[bool, str]:
    """Whether we are willing to send an order in this name right now."""
    if quote.tradability is Tradability.HALTED:
        return False, "exchange halt or suspension"
    if quote.tradability is Tradability.STALE:
        return False, "quote is stale"
    if quote.tradability is Tradability.AT_PRICE_LIMIT:
        return False, "printing at the daily price limit"

    move = quote.move_from_prev_close
    threshold = price_limit_pct * LIMIT_BAND_PROXIMITY
    if buying and move >= threshold:
        return False, (
            f"up {move:.1%} from prev close, within the "
            f"{price_limit_pct:.0%} limit band; not chasing"
        )
    if not buying and move <= -threshold:
        return False, (
            f"down {move:.1%} from prev close, within the "
            f"{price_limit_pct:.0%} limit band; not dumping into a limit"
        )
    return True, ""


def _fit_to_budget(
    order: ProposedOrder,
    budget: Decimal,
    *,
    lot_size: int,
    min_notional: Decimal,
    constraint: str,
) -> tuple[Optional[ProposedOrder], str]:
    """Shrink `order` to fit `budget`, or decline it if it cannot usefully fit.

    Returns the original order untouched when it already fits, a trimmed copy
    when a partial correction is still worth sending, and None when what is left
    of the budget cannot support an economic order.
    """
    if budget <= 0:
        return None, f"{constraint} exhausted"
    if order.notional <= budget:
        return order, ""
    trimmed_qty = _round_to_lot(budget / order.limit_price, lot_size)
    if trimmed_qty <= 0:
        return None, f"{constraint} leaves less than one lot ({budget:.0f} EGP)"
    if trimmed_qty * order.limit_price < min_notional:
        return None, (
            f"{constraint} leaves {budget:.0f} EGP, below the "
            f"{min_notional:.0f} EGP order floor"
        )
    return (
        order.scaled_to(trimmed_qty, f"{constraint} capped this at {budget:.0f} EGP"),
        "",
    )


def _has_rested(
    symbol: str,
    last_traded: Mapping[str, date],
    today: date,
    cooldown_days: int,
) -> tuple[bool, str]:
    if cooldown_days <= 0:
        return True, ""
    previous = last_traded.get(symbol)
    if previous is None:
        return True, ""
    elapsed = (today - previous).days
    if elapsed < cooldown_days:
        return False, f"traded {elapsed}d ago, {cooldown_days}d cooldown"
    return True, ""


def _limit_price(quote: Quote, side: Side) -> Decimal:
    """Limit priced through the last print by a fixed slippage allowance."""
    factor = (
        Decimal(1) + LIMIT_SLIPPAGE if side is Side.BUY else Decimal(1) - LIMIT_SLIPPAGE
    )
    return (quote.last * factor).quantize(Decimal("0.001"))


def _round_to_lot(quantity: Decimal, lot_size: int) -> Decimal:
    lots = (quantity / Decimal(lot_size)).to_integral_value(rounding=ROUND_DOWN)
    return lots * Decimal(lot_size)

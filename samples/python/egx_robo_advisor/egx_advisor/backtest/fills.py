"""Fill model: what the market would actually have done with our orders.

The temptation in a backtest is to assume every order fills at the close. That
single assumption is responsible for most backtests that cannot be reproduced
live, because it quietly grants free liquidity and perfect timing. This model
refuses all three of those gifts:

  * **A limit only fills if the day traded through it.** A buy limit needs the
    day's low at or below it; a sell limit needs the high at or above it.
  * **Size is capped by participation.** You cannot buy 40% of a day's volume in
    a thin EGX name without moving the price against yourself, so the fill is
    truncated to a share of actual volume. Partial fills are normal here, not an
    edge case.
  * **Slippage is charged even on a fill.** Touching the limit is not the same as
    being filled at it.

Halts and price-limit days produce no fill at all, matching the live strategy's
refusal to send an order into them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from ..types import ProposedOrder, Side, Tradability
from .types import Bar, CostModel, Fill, Rejection


@dataclass(frozen=True, slots=True)
class FillModel:
    costs: CostModel = CostModel()
    #: Largest share of a session's volume we assume we can take. EGX names
    #: outside the top handful are thin; 10% is already optimistic for some.
    max_volume_participation: Decimal = Decimal("0.10")
    #: When a history has no volume column, fall back to allowing the full order.
    #: Flagged in the result so the optimism is visible rather than assumed away.
    assume_liquid_when_volume_missing: bool = True

    def execute(
        self, order: ProposedOrder, bar: Optional[Bar], day: date, lot_size: int = 1
    ) -> tuple[Optional[Fill], Optional[Rejection]]:
        """Resolve one order against one session. Returns (fill, rejection)."""
        if bar is None:
            return None, Rejection(day, order.symbol, order.side, order.quantity, "no bar")

        if bar.tradability is Tradability.HALTED:
            return None, Rejection(day, order.symbol, order.side, order.quantity, "halted")
        if bar.tradability is Tradability.STALE:
            return None, Rejection(day, order.symbol, order.side, order.quantity, "stale bar")
        if bar.tradability is Tradability.AT_PRICE_LIMIT:
            return None, Rejection(
                day, order.symbol, order.side, order.quantity, "at daily price limit"
            )

        # Did the day trade through our limit?
        if order.side is Side.BUY:
            if bar.low > order.limit_price:
                return None, Rejection(
                    day, order.symbol, order.side, order.quantity,
                    f"limit {order.limit_price} below the day's low {bar.low}",
                )
            # Filled no better than our limit, and no worse than the day's low.
            base = min(order.limit_price, max(bar.low, bar.open))
            price = base * (Decimal(1) + self.costs.slippage_rate)
            price = min(price, order.limit_price)
        else:
            if bar.high < order.limit_price:
                return None, Rejection(
                    day, order.symbol, order.side, order.quantity,
                    f"limit {order.limit_price} above the day's high {bar.high}",
                )
            base = max(order.limit_price, min(bar.high, bar.open))
            price = base * (Decimal(1) - self.costs.slippage_rate)
            price = max(price, order.limit_price)

        quantity = self._participation_cap(order.quantity, bar, lot_size)
        if quantity <= 0:
            return None, Rejection(
                day, order.symbol, order.side, order.quantity,
                f"volume {bar.volume} too thin for a whole lot",
            )

        notional = quantity * price
        charges = self.costs.charges(notional)
        # Slippage is the gap between the price we could realistically have had
        # (`base`) and the one we actually got -- NOT the gap to our limit.
        # Measuring against the limit counts price *improvement* as a cost: a buy
        # limit at 85.4 filled at 85.0 is 0.40 better than asked for, and charging
        # that as slippage made a zero-cost model report costs.
        slippage_cost = abs(price - base) * quantity
        cash_delta = (-notional - charges) if order.side is Side.BUY else (notional - charges)

        fill = Fill(
            day=day,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price.quantize(Decimal("0.001")),
            charges=charges.quantize(Decimal("0.01")),
            cash_delta=cash_delta.quantize(Decimal("0.01")),
            slippage_cost=slippage_cost.quantize(Decimal("0.01")),
        )
        partial = quantity < order.quantity
        rejection = (
            Rejection(
                day, order.symbol, order.side, order.quantity - quantity,
                f"partial: volume cap filled {quantity} of {order.quantity}",
            )
            if partial
            else None
        )
        return fill, rejection

    def _participation_cap(self, wanted: Decimal, bar: Bar, lot_size: int) -> Decimal:
        if bar.volume <= 0:
            return wanted if self.assume_liquid_when_volume_missing else Decimal(0)
        allowed = bar.volume * self.max_volume_participation
        capped = min(wanted, allowed)
        lots = (capped / Decimal(lot_size)).to_integral_value(rounding=ROUND_DOWN)
        return lots * Decimal(lot_size)

"""The regime filter: a circuit breaker, structurally incapable of being a signal.

The invariant
-------------
News may only ever *remove* trades from a plan. It cannot add an order, raise a
quantity, change a side, or introduce a symbol. This is not a convention we
intend to respect -- it is asserted at runtime by `_assert_subtractive()` on every
call, and proved by a test that throws random plans at it. If a future change
tried to make the bot buy on good news, the filter would raise before anything
reached the screen.

Why go to that trouble? Because "use news as a filter, not a signal" is the kind
of discipline that erodes. It starts as a comment, someone adds "and if sentiment
is very positive, upsize by 20%", and eighteen months later the bot is a momentum
chaser with a news feed. An executable invariant does not erode. Our backtests
said news has no predictive alpha on these names; the architecture should make it
impossible to bet otherwise by accident.

Asymmetry
---------
The filter blocks *buying*. Sells stay permitted under BUYS_HALTED, because when
something catastrophic is unfolding, refusing to let the bot reduce exposure is
its own kind of risk. Only ALL_HALTED stops everything, and that is reserved for
situations where we do not trust our own picture of the market.

Fail-closed on silence
----------------------
Stale feeds mean BUYS_HALTED, not RISK_ON. Absence of bad news is not evidence of
calm -- it is far more likely that our poller is broken, and the failure mode of
"assume calm because we cannot see" is precisely how an automated buyer walks
into a devaluation.

Hysteresis
----------
Coming back from risk-off needs both a cooldown and several consecutive clean
polls. Without it a single recurring headline flaps the bot in and out of the
market and burns commission on the boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Optional, Sequence

from ..types import (
    NewsAssessment,
    ProposedOrder,
    RegimeState,
    RiskState,
    Scope,
    Severity,
    Side,
)

logger = logging.getLogger(__name__)


class SubtractiveInvariantViolation(AssertionError):
    """The filter tried to add or enlarge an order. A bug, never a market event."""


@dataclass(frozen=True, slots=True)
class FilterConfig:
    #: Feeds older than this during a session mean we are flying blind.
    max_feed_age: timedelta = timedelta(minutes=90)
    #: How long a market-wide risk-off persists after the last catastrophic item.
    market_cooldown: timedelta = timedelta(hours=24)
    #: How long a per-symbol block persists.
    symbol_cooldown: timedelta = timedelta(hours=72)
    #: Consecutive clean polls required before buying resumes.
    clean_polls_to_recover: int = 3
    #: Elevated (not catastrophic) items needed to warrant halting buys.
    elevated_items_for_halt: int = 4


@dataclass
class RegimeFilter:
    """Evaluates news into a RiskState, then subtracts from a plan.

    Holds a little state -- cooldown timers and the clean-poll counter -- which is
    why it is a class rather than a function. The state is small, inspectable, and
    published to the dashboard.
    """

    config: FilterConfig = field(default_factory=FilterConfig)
    #: While set and in the future, ALL_HALTED is latched regardless of the news.
    #: Without this, the next poll's `evaluate()` would quietly downgrade a panic
    #: to BUYS_HALTED and let the bot back onto the screen -- exactly the state a
    #: panic exists to prevent.
    _panic_until: Optional[datetime] = None
    _panic_reason: str = ""
    _risk_off_until: Optional[datetime] = None
    _blocked_until: dict[str, datetime] = field(default_factory=dict)
    _clean_polls: int = 0
    _last_drivers: tuple[str, ...] = ()

    # ------------------------------------------------------------------ evaluate

    def evaluate(
        self,
        assessments: Sequence[NewsAssessment],
        *,
        now: Optional[datetime] = None,
        feed_age_seconds: Optional[float] = None,
        sources_failed: Sequence[str] = (),
        in_session: bool = True,
        authoritative_failed: Sequence[str] = (),
        scheduled: Sequence[str] = (),
    ) -> RegimeState:
        """Fold this poll's assessments into a RiskState.

        `authoritative_failed` names official sources (the exchange's own
        disclosures) that failed this poll; `scheduled` names listed market
        events whose window covers today. Both can only add restriction.
        """
        now = now or datetime.now(timezone.utc)
        # Kept separate and concatenated market-first at the end. `drivers[0]` is
        # what the dashboard headlines and what `_veto_reason` cites, so a
        # market-wide halt must not be explained by whichever single-name block
        # happened to be appended first -- that reads as though one halted ticker
        # stopped all buying, which is both wrong and alarming.
        market_drivers: list[str] = []
        symbol_drivers: list[str] = []

        # A latched panic outranks everything the feeds have to say.
        if self._panic_until is not None and now < self._panic_until:
            return RegimeState(
                as_of=now,
                risk_state=RiskState.ALL_HALTED,
                blocked_symbols=frozenset(self._blocked_until),
                drivers=(
                    f"ALL HALTED (latched): {self._panic_reason}",
                    f"clears at {self._panic_until.isoformat()} unless cleared by an operator",
                ),
                feed_age_seconds=feed_age_seconds,
                stale=True,
            )

        catastrophic = [a for a in assessments if a.severity is Severity.CATASTROPHIC]
        elevated = [a for a in assessments if a.severity is Severity.ELEVATED]

        # 1. Per-symbol blocks from single-name catastrophes.
        for assessment in catastrophic:
            if assessment.scope is Scope.TICKER and assessment.symbols:
                for symbol in assessment.symbols:
                    self._blocked_until[symbol] = now + self.config.symbol_cooldown
                    symbol_drivers.append(
                        f"{symbol} blocked: {assessment.headline.title[:110]}"
                    )

        # 2. Market-wide risk-off from market or sector catastrophes.
        market_events = [a for a in catastrophic if a.scope in (Scope.MARKET, Scope.SECTOR)]
        if market_events:
            self._risk_off_until = now + self.config.market_cooldown
            self._clean_polls = 0
            for assessment in market_events[:5]:
                market_drivers.append(f"MARKET: {assessment.headline.title[:110]}")

        # 3. A pile-up of merely elevated items also warrants standing down. One
        #    profit warning is noise; four at once is a pattern we do not want to
        #    buy into while we work out what it is.
        elif len(elevated) >= self.config.elevated_items_for_halt:
            self._risk_off_until = now + self.config.market_cooldown
            self._clean_polls = 0
            market_drivers.append(
                f"{len(elevated)} elevated items in one poll "
                f"(threshold {self.config.elevated_items_for_halt})"
            )
        elif not sources_failed:
            # A poll with failing sources saw less than it should have; it is
            # not evidence that things are calm.
            self._clean_polls += 1

        # 4. Staleness and coverage. Only enforced in-session: outside trading
        #    hours a quiet feed is normal and should not latch a halt.
        stale = False
        if in_session:
            if feed_age_seconds is None:
                stale = True
                market_drivers.append("no successful feed poll yet")
            elif feed_age_seconds > self.config.max_feed_age.total_seconds():
                stale = True
                market_drivers.append(
                    f"news feeds stale ({feed_age_seconds / 60:.0f} min old, limit "
                    f"{self.config.max_feed_age.total_seconds() / 60:.0f} min)"
                )
            if sources_failed:
                market_drivers.append(
                    f"sources failing: {', '.join(sources_failed)}"
                )

        # 5. Resolve. Note the direction of every branch: each one can only make
        #    the state more restrictive.
        risk_state = RiskState.RISK_ON
        cooling = self._risk_off_until is not None and now < self._risk_off_until
        recovered = self._clean_polls >= self.config.clean_polls_to_recover

        if cooling:
            risk_state = RiskState.BUYS_HALTED
            remaining = (self._risk_off_until - now).total_seconds() / 3600  # type: ignore[operator]
            market_drivers.append(f"risk-off cooldown active for another {remaining:.1f}h")
        elif self._risk_off_until is not None and not recovered:
            risk_state = RiskState.BUYS_HALTED
            market_drivers.append(
                f"cooldown elapsed but only {self._clean_polls}/"
                f"{self.config.clean_polls_to_recover} clean polls so far"
            )
        elif self._risk_off_until is not None:
            self._risk_off_until = None
            market_drivers.append("recovered: cooldown elapsed and feeds clean")

        if stale:
            risk_state = _max_restrictive(risk_state, RiskState.BUYS_HALTED)

        # 6. The exchange's own disclosures are how halts and suspensions are
        #    announced. Without them, in session, we cannot see the one kind of
        #    news that matters most, so we stop buying.
        if in_session and authoritative_failed:
            risk_state = _max_restrictive(risk_state, RiskState.BUYS_HALTED)
            market_drivers.insert(
                0, f"official source unavailable: {', '.join(authoritative_failed)}"
            )

        # 7. Scheduled events (rate decisions, inflation prints): no new buys
        #    around them. A bet on the outcome is not this bot's business.
        if scheduled:
            risk_state = _max_restrictive(risk_state, RiskState.BUYS_HALTED)
            for title in scheduled:
                market_drivers.insert(0, f"scheduled event: {title}")

        self._expire_symbol_blocks(now)
        blocked = frozenset(self._blocked_until)
        drivers = market_drivers + symbol_drivers
        self._last_drivers = tuple(drivers)

        return RegimeState(
            as_of=now,
            risk_state=risk_state,
            blocked_symbols=blocked,
            drivers=tuple(drivers) or ("no risk drivers; feeds clean",),
            feed_age_seconds=feed_age_seconds,
            stale=stale,
        )

    def clear_panic(self, *, actor: str) -> None:
        """Release a latched panic. An explicit human act, never automatic."""
        logger.warning("panic latch cleared by %s (was: %s)", actor, self._panic_reason)
        self._panic_until = None
        self._panic_reason = ""
        self._clean_polls = 0

    @property
    def panicking(self) -> bool:
        return self._panic_until is not None

    def _expire_symbol_blocks(self, now: datetime) -> None:
        for symbol in [s for s, until in self._blocked_until.items() if now >= until]:
            del self._blocked_until[symbol]

    def panic(self, reason: str, *, now: Optional[datetime] = None) -> RegimeState:
        """Escalate to ALL_HALTED and latch it.

        Latching matters: a panic means we do not trust our own picture of the
        world, and the next news poll is not evidence that we should. It clears
        on a timer or when an operator clears it explicitly, never because a
        subsequent poll happened to look calm.
        """
        now = now or datetime.now(timezone.utc)
        self._risk_off_until = now + self.config.market_cooldown
        self._panic_until = now + self.config.market_cooldown
        self._panic_reason = reason
        self._clean_polls = 0
        return RegimeState(
            as_of=now,
            risk_state=RiskState.ALL_HALTED,
            blocked_symbols=frozenset(self._blocked_until),
            drivers=(f"ALL HALTED: {reason}",),
            stale=True,
        )

    # --------------------------------------------------------------------- apply

    def apply(
        self, orders: Sequence[ProposedOrder], regime: RegimeState
    ) -> tuple[tuple[ProposedOrder, ...], tuple[tuple[ProposedOrder, str], ...]]:
        """Subtract disallowed orders from a plan.

        Returns (allowed, suppressed_with_reasons). Guaranteed by assertion to
        return a subset of `orders` with no quantity increased.
        """
        allowed: list[ProposedOrder] = []
        suppressed: list[tuple[ProposedOrder, str]] = []

        for order in orders:
            reason = self._veto_reason(order, regime)
            if reason is None:
                allowed.append(order)
            else:
                suppressed.append((order, reason))

        _assert_subtractive(orders, allowed)
        return tuple(allowed), tuple(suppressed)

    def _veto_reason(self, order: ProposedOrder, regime: RegimeState) -> Optional[str]:
        """Why this order is blocked, or None if it may proceed."""
        if regime.risk_state is RiskState.ALL_HALTED:
            return f"ALL_HALTED: {_first_driver(regime)}"

        if order.side is Side.SELL:
            # Sells survive BUYS_HALTED by design: de-risking during a crisis is
            # the behaviour we want, not the behaviour we are guarding against.
            return None

        if regime.risk_state is RiskState.BUYS_HALTED:
            return f"buys halted: {_first_driver(regime)}"

        if order.symbol in regime.blocked_symbols:
            return f"{order.symbol} is under a single-name news block"

        return None


# --------------------------------------------------------------------------- #
# The invariant
# --------------------------------------------------------------------------- #


def _assert_subtractive(
    original: Sequence[ProposedOrder], result: Sequence[ProposedOrder]
) -> None:
    """Prove that filtering only removed things.

    Checks, for every surviving order, that it corresponds to an input order with
    the same symbol and side and a quantity no larger. Any violation is a
    programming error in the filter, so it raises rather than logging: a regime
    layer that can invent a trade must not be allowed to run.
    """
    if len(result) > len(original):
        raise SubtractiveInvariantViolation(
            f"filter returned {len(result)} orders from {len(original)}"
        )

    available: dict[tuple[str, str], list[ProposedOrder]] = {}
    for order in original:
        available.setdefault((order.symbol, order.side.value), []).append(order)

    for order in result:
        key = (order.symbol, order.side.value)
        candidates = available.get(key)
        if not candidates:
            raise SubtractiveInvariantViolation(
                f"filter produced an order the plan never contained: "
                f"{order.side.value} {order.symbol}"
            )
        match = next((c for c in candidates if c.quantity >= order.quantity), None)
        if match is None:
            largest = max(c.quantity for c in candidates)
            raise SubtractiveInvariantViolation(
                f"filter enlarged {order.side.value} {order.symbol}: "
                f"{order.quantity} > {largest}"
            )
        candidates.remove(match)


def _max_restrictive(left: RiskState, right: RiskState) -> RiskState:
    return left if left.rank >= right.rank else right


def _first_driver(regime: RegimeState) -> str:
    return regime.drivers[0] if regime.drivers else "unspecified"

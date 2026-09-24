"""The agent daemon: one loop, five gates, nothing clever.

Cycle order, and why it is this order
-------------------------------------
Each cycle runs the cheap, safe, offline checks first and only then reaches for
the screen. Every gate can stop the cycle, and they are arranged so that the ones
that cost nothing and risk nothing come first::

    1. kill switch      (a file read)        -> stop
    2. trading calendar (arithmetic)         -> idle until the next session
    3. news + regime    (network, no screen) -> may forbid buying, or everything
    4. demo assertion   (screen, read-only)  -> may forbid clicking
    5. plan + execute   (clicks)             -> per-order re-assertion

Gate 3 finishing before gate 4 begins is the property the whole design turns on:
**the news layer decides what is permitted before the bot is allowed to touch the
screen at all.** It is structural, not conventional. `_run_cycle` computes the
`RegimeState` and, on ALL_HALTED, returns before `_ensure_connected()` is ever
called -- so in that state no interface exists, no screenshot is taken, and there
is no object in scope capable of clicking anything.

The plan is produced by the pure strategy layer with no knowledge of the news, and
the regime filter then subtracts from it. Keeping generation and suppression apart
is what lets each be tested on its own, and is what makes "news can only remove
trades" checkable rather than aspirational.

Communication with the dashboard
--------------------------------
This process is the sole writer of events and snapshots, and a reader of control.
The dashboard is the mirror image. Neither imports the other; the bus file is the
entire contract. See `bus.py` for why that boundary is drawn where it is.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from .bus import EventKind, StateBus
from .clock import TZDATA_AVAILABLE, TradingCalendar
from .execution.thndr import (
    ExecutionError,
    GuardedComputer,
    PortfolioReadError,
    ThndrExecutor,
    ThndrUiMap,
)
from .marketdata import MarketDataError, MarketDataProvider
from .regime.filter import RegimeFilter
from .regime.sentiment import Classifier, KeywordClassifier, LlmClassifier, combine
from .regime.sources import NewsFetcher
from .safety.demo_guard import DemoGuard, DemoModeViolation
from .safety.modes import ExecutionMode, OrderTicketsForbidden
from .safety.guarded_interface import GuardedInterface, KillSwitchEngaged
from .strategy.policy import AllocationPolicy
from .strategy.rebalance import plan_rebalance
from .types import AgentPhase, RegimeState, RiskState

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Everything the loop needs that is not code."""

    bus_path: str = "state/egx_bus.db"
    #: Seconds between cycles while the market is open.
    cycle_interval: float = 300.0
    #: Seconds between polls while out of session or after an error.
    idle_interval: float = 120.0
    #: Seconds between checks for an arm while halted. Short, so the bot
    #: answers the dashboard's START within a couple of seconds rather than
    #: after a full idle interval.
    halted_poll_interval: float = 1.0
    #: Seconds between being armed and the first screenshot, so the operator
    #: can bring the broker window to the front -- pressing START leaves the
    #: dashboard there. Halting during the countdown cancels it.
    arm_delay: float = field(
        default_factory=lambda: float(os.environ.get("EGX_ARM_DELAY", "10"))
    )
    #: Cap on orders per cycle, independent of the turnover cap. A second
    #: backstop against a runaway plan, expressed in clicks rather than EGP.
    max_orders_per_cycle: int = 4
    #: Set False only to run the loop with execution disabled (dry run).
    execute_orders: bool = True
    #: What the bot may do, and on whose account. See safety/modes.py.
    mode: ExecutionMode = ExecutionMode.SIMULATOR_ONLY
    ui: ThndrUiMap = field(default_factory=ThndrUiMap)
    policy: AllocationPolicy = field(default_factory=AllocationPolicy)
    holidays: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        # A mode that cannot touch an order at all must not be paired with
        # execution enabled. Reconciling it here means the rest of the loop never
        # has to ask which of the two settings wins.
        #
        # The test is `permits_order_tickets`, not `permits_orders`: on
        # LIVE_PREPARE_ONLY the loop has real work to do -- filling tickets for
        # the operator to submit -- so switching execution off there would make
        # the rung inert.
        if self.execute_orders and not self.mode.permits_order_tickets:
            self.execute_orders = False


class EgxCuaAgent:
    """Owns the loop, the gates, and the bus.

    Construct with an already-built `Computer` and `ComputerAgent` factory so that
    this class stays testable: every external edge -- screen, prices, news -- is an
    injected collaborator.
    """

    def __init__(
        self,
        *,
        config: AgentConfig,
        computer: Any,
        market_data: MarketDataProvider,
        agent_factory: Optional[Any] = None,
        news_fetcher: Optional[NewsFetcher] = None,
        classifiers: Optional[Sequence[Classifier]] = None,
        regime_filter: Optional[RegimeFilter] = None,
        demo_guard: Optional[DemoGuard] = None,
        bus: Optional[StateBus] = None,
        vision_completion: Optional[Any] = None,
        executor_factory: Optional[Any] = None,
    ) -> None:
        self.config = config
        #: Builds the executor from the bus instead of from `computer`. The desktop
        #: app's browser target uses it; the screen targets leave it None.
        self._executor_factory = executor_factory
        self._vision_completion = vision_completion
        self._computer = computer
        self._market_data = market_data
        self._agent_factory = agent_factory
        self.bus = bus or StateBus(config.bus_path)
        self.news = news_fetcher or NewsFetcher()
        self.classifiers: tuple[Classifier, ...] = tuple(
            classifiers
            if classifiers is not None
            else (
                KeywordClassifier(aliases=_aliases(config.policy)),
                LlmClassifier(),
            )
        )
        self.regime_filter = regime_filter or RegimeFilter()
        self.guard = demo_guard or DemoGuard.default()
        self.calendar = TradingCalendar(holidays=config.holidays)

        self._interface: Optional[GuardedInterface] = None
        self._executor: Optional[ThndrExecutor] = None
        self._regime: RegimeState = RegimeState.unknown()
        self._last_traded: dict[str, date] = {}
        self._last_feed_ok: Optional[datetime] = None
        self._stop = asyncio.Event()
        #: False until the post-arm countdown has run; reset whenever halted.
        self._screen_ready = False

    # ------------------------------------------------------------------ lifecycle

    async def run_forever(self) -> None:
        """Main loop. Runs until stopped; never exits on a per-cycle error."""
        self._announce_startup()
        while not self._stop.is_set():
            phase = AgentPhase.IDLE
            try:
                phase = await self._run_cycle()
            except KillSwitchEngaged as exc:
                self.bus.publish(EventKind.CONTROL, f"cycle stopped: {exc}", phase="halted")
                phase = AgentPhase.HALTED
            except OrderTicketsForbidden as exc:
                # Expected on an observe-only rung: the plan reached execution
                # and was refused by design. Not an error, and not a halt.
                self.bus.publish(
                    EventKind.CONTROL, f"order refused by mode: {exc}", phase="idle"
                )
                phase = AgentPhase.PLANNING
            except DemoModeViolation as exc:
                # The guard has already halted the bus if it saw real money. Do not
                # retry: the next cycle re-checks the kill switch and will find it.
                self.bus.publish(
                    EventKind.ERROR, f"demo-mode violation: {exc}", phase="halted"
                )
                phase = AgentPhase.HALTED
            except (MarketDataError, PortfolioReadError) as exc:
                # We do not understand our own inputs, so this cycle plans nothing
                # and the next one reads them again. This used to latch a 24-hour
                # panic, which nothing could clear short of a restart: one
                # illegible screenshot or one missing Yahoo row stopped the bot
                # for a day. A failed read already produces no plan, and a plan
                # is the only way to an order, so retrying is the safe choice.
                self.bus.publish(
                    EventKind.ERROR,
                    f"could not read inputs, will retry next cycle: {exc}",
                    phase="error",
                )
                phase = AgentPhase.ERROR
            except ExecutionError as exc:
                self.bus.publish(EventKind.ERROR, f"execution failed: {exc}", phase="error")
                phase = AgentPhase.ERROR
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                logger.exception("unhandled error in cycle")
                self.bus.publish(EventKind.ERROR, f"unhandled: {exc!r}", phase="error")
                phase = AgentPhase.ERROR

            # Wake early only when the bus itself is halted. A cycle can also end
            # in HALTED with the bus armed (a guard refusal that did not latch
            # the kill switch); waking early there would re-run the cycle at
            # once, in a tight loop against the screen and the model.
            if phase is AgentPhase.HALTED and self.bus.is_halted():
                await self._wait_for_arm(self.config.idle_interval)
                continue
            delay = (
                self.config.cycle_interval
                if phase in (AgentPhase.EXECUTING, AgentPhase.PLANNING)
                else self.config.idle_interval
            )
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _wait_for_arm(self, timeout: float) -> None:
        """Sleep while halted, waking as soon as the bus is armed or stop is set."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self._stop.is_set() and loop.time() < deadline:
            if not self.bus.is_halted():
                return
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.config.halted_poll_interval
                )

    async def _arm_countdown(self) -> bool:
        """Give the operator time to bring the broker to the front. False if halted."""
        delay = max(0.0, self.config.arm_delay)
        if delay <= 0:
            return True
        self.bus.publish(
            EventKind.CONTROL,
            f"armed: first screenshot in {delay:.0f} s -- bring Thndr X to the front "
            f"and keep the dashboard out of view",
            phase="countdown",
        )
        self.bus.put(
            "status",
            {"phase": "countdown", "reason": f"first screenshot in {delay:.0f} s"},
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + delay
        while loop.time() < deadline:
            if self.bus.is_halted() or self._stop.is_set():
                self.bus.publish(
                    EventKind.CONTROL, "countdown cancelled: halted", phase="halted"
                )
                return False
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=min(0.5, deadline - loop.time())
                )
        return not self.bus.is_halted()

    def request_stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Ctrl-C and SIGTERM stop the loop *and* halt the bus.

        A process going away is not the same as a bot that is safe to restart
        unattended, so shutdown latches the kill switch. Arming again is an
        explicit human action.
        """
        loop = asyncio.get_running_loop()

        def _handler(signame: str) -> None:
            self.bus.halt(actor=f"signal:{signame}", reason="process shutting down")
            self.request_stop()

        for signame in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, signame, None)
            if sig is None:
                continue
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, _handler, signame)

    def _announce_startup(self) -> None:
        # Every start is a halted start. The control row outlives the process,
        # and on Windows Ctrl-C does not run the signal handler that halts it,
        # so an agent stopped while armed used to come back armed. Starting is
        # the operator's act, every time.
        if not self.bus.is_halted():
            self.bus.halt(
                actor="system",
                reason="agent (re)started; arm from the dashboard to begin",
            )
        state = self.bus.control_state()
        self.bus.publish(
            EventKind.LIFECYCLE,
            f"agent started -- {self.config.mode.banner}",
            phase="idle",
            data={
                "halted": state.halted,
                "halt_reason": state.reason,
                "mode": self.config.mode.value,
                "execute_orders": self.config.execute_orders,
                "calibrated": self.config.ui.calibration_complete,
                "tzdata": TZDATA_AVAILABLE,
            },
        )
        if not TZDATA_AVAILABLE:
            self.bus.publish(
                EventKind.ERROR,
                "tzdata unavailable: EGX session window falls back to fixed UTC+2 and "
                "will be an hour off during Cairo DST. Install tzdata.",
                phase="idle",
            )
        if state.halted:
            self.bus.publish(
                EventKind.CONTROL,
                f"starting HALTED ({state.reason}); arm from the dashboard to trade",
                phase="halted",
            )

    # ---------------------------------------------------------------- the cycle

    async def _run_cycle(self) -> AgentPhase:
        now = datetime.now(timezone.utc)

        # --- Gate 1: kill switch ------------------------------------------------
        # Published every cycle, not just at startup: an operator opening the
        # dashboard mid-run must never have to guess which account is being
        # driven, and a stale banner from a previous run would be worse than none.
        self.bus.put(
            "mode",
            {
                "mode": self.config.mode.value,
                "banner": self.config.mode.banner,
                "live": self.config.mode.permits_live_account,
                "can_order": self.config.mode.permits_orders,
                "execute_orders": self.config.execute_orders,
            },
        )

        if self.bus.is_halted():
            # The next arm must count down again before the screen is read.
            self._screen_ready = False
            state = self.bus.control_state()
            self.bus.put("status", {"phase": AgentPhase.HALTED.value, "reason": state.reason})
            return AgentPhase.HALTED

        # --- Gate 2: trading calendar ------------------------------------------
        session = self.calendar.state_at(now)
        if not session.can_trade:
            next_open = self.calendar.next_session_open(now)
            self.bus.put(
                "status",
                {
                    "phase": AgentPhase.IDLE.value,
                    "session": session.value,
                    "next_open": next_open.isoformat(),
                },
            )
            # News still polls out of session so the dashboard stays informative
            # and the regime has a warm view before the open.
            await self._refresh_regime(now, in_session=False)
            return AgentPhase.IDLE

        # --- Gate 3: news and regime, BEFORE any screen access -----------------
        self.bus.put("status", {"phase": AgentPhase.POLLING_NEWS.value, "session": session.value})
        regime = await self._refresh_regime(now, in_session=True)

        if regime.risk_state is RiskState.ALL_HALTED:
            # Return before `_ensure_connected()`: in this state no interface is
            # constructed, so there is nothing in scope that could click.
            self.bus.publish(
                EventKind.REGIME,
                f"ALL_HALTED, not touching the screen: {regime.drivers[0] if regime.drivers else ''}",
                phase="halted",
            )
            self.bus.put("status", {"phase": AgentPhase.HALTED.value, "reason": "regime ALL_HALTED"})
            return AgentPhase.HALTED

        # --- Countdown: the first screenshot after an arm waits for the operator.
        if not self._screen_ready:
            if not await self._arm_countdown():
                return AgentPhase.HALTED
            self._screen_ready = True

        # --- Gate 4: demo assertion --------------------------------------------
        self.bus.put("status", {"phase": AgentPhase.ASSERTING_DEMO.value})
        executor = await self._ensure_connected()
        await executor.ensure_simulator()

        # --- Read state ---------------------------------------------------------
        self.bus.put("status", {"phase": AgentPhase.READING_PORTFOLIO.value})
        portfolio = await executor.read_portfolio()
        market = await self._market_data.snapshot(self.config.policy.universe)

        # --- Gate 5: plan, filter, execute -------------------------------------
        self.bus.put("status", {"phase": AgentPhase.PLANNING.value})
        plan = plan_rebalance(
            portfolio=portfolio,
            market=market,
            policy=self.config.policy,
            regime=regime,
            last_traded=self._last_traded,
            today=now.date(),
        )
        allowed, suppressed = self.regime_filter.apply(plan.orders, regime)
        self._publish_plan(plan, allowed, suppressed)

        if not allowed:
            self.bus.put("status", {"phase": AgentPhase.IDLE.value, "reason": "nothing to do"})
            return AgentPhase.PLANNING

        if not self.config.execute_orders:
            self.bus.publish(
                EventKind.PLAN,
                f"dry run: {len(allowed)} order(s) withheld (execute_orders=False)",
                phase="planning",
            )
            return AgentPhase.PLANNING

        self.bus.put("status", {"phase": AgentPhase.EXECUTING.value})
        executed = 0
        for order in allowed[: self.config.max_orders_per_cycle]:
            # Re-check the kill switch between orders: the operator may have hit it
            # from their phone while the previous order was being placed, and each
            # order is a separate decision to keep going.
            if self.bus.is_halted():
                self.bus.publish(
                    EventKind.CONTROL,
                    f"kill switch engaged mid-cycle; {len(allowed) - executed} order(s) abandoned",
                    phase="halted",
                )
                break
            if self.config.mode.permits_orders:
                await executor.submit_order(order)
            else:
                # The rung fills the ticket and stops. One per cycle is not a
                # limit worth working around: a second ticket would overwrite the
                # first on screen before anyone had looked at it.
                await executor.prepare_order(order)
                self._last_traded[order.symbol] = now.date()
                executed += 1
                break
            self._last_traded[order.symbol] = now.date()
            executed += 1

        verb = "submitted" if self.config.mode.permits_orders else "prepared for you"
        self.bus.publish(
            EventKind.LIFECYCLE,
            f"cycle complete: {executed} order(s) {verb}",
            phase="executing",
            data={
                "executed": executed,
                "planned": len(allowed),
                "submitted": self.config.mode.permits_orders,
            },
        )
        return AgentPhase.EXECUTING

    # ------------------------------------------------------------------- regime

    async def _refresh_regime(self, now: datetime, *, in_session: bool) -> RegimeState:
        """Poll news, classify, and fold into a RiskState. Never touches the screen."""
        poll = await self.news.poll(now=now)
        if poll.headlines or not poll.failures:
            self._last_feed_ok = now

        assessments = combine(
            *[self._classify_safely(c, poll.headlines) for c in self.classifiers]
        )

        feed_age = (
            (now - self._last_feed_ok).total_seconds() if self._last_feed_ok else None
        )
        regime = self.regime_filter.evaluate(
            assessments,
            now=now,
            feed_age_seconds=feed_age,
            sources_failed=tuple(name for name, _ in poll.failures),
            in_session=in_session,
        )
        self._regime = regime
        self._publish_regime(poll_failures=poll.failures, headline_count=len(poll.headlines))
        return regime

    def _classify_safely(self, classifier: Classifier, headlines: Sequence[Any]):
        """A classifier that raises must not take the regime layer down with it."""
        try:
            return classifier.classify(headlines)
        except Exception as exc:  # noqa: BLE001
            logger.warning("classifier %s failed: %s", classifier.name, exc)
            self.bus.publish(
                EventKind.ERROR, f"classifier {classifier.name} failed: {exc}", phase="polling_news"
            )
            return ()

    def _publish_regime(
        self,
        *,
        poll_failures: Sequence[tuple[str, str]] = (),
        headline_count: int = 0,
    ) -> None:
        regime = self._regime
        self.bus.put(
            "regime",
            {
                "as_of": regime.as_of.isoformat(),
                "risk_state": regime.risk_state.value,
                "blocked_symbols": sorted(regime.blocked_symbols),
                "drivers": list(regime.drivers),
                "feed_age_seconds": regime.feed_age_seconds,
                "stale": regime.stale,
                "headline_count": headline_count,
                "source_failures": [{"source": n, "error": e} for n, e in poll_failures],
            },
        )
        self.bus.publish(
            EventKind.REGIME,
            f"{regime.risk_state.value.upper()} "
            f"({regime.drivers[0] if regime.drivers else 'no drivers'})",
            phase="polling_news",
            data={"blocked": sorted(regime.blocked_symbols)},
        )

    def _publish_plan(self, plan: Any, allowed: Sequence[Any], suppressed: Sequence[Any]) -> None:
        self.bus.put(
            "plan",
            {
                "as_of": plan.as_of.isoformat(),
                "policy_state": plan.policy_state,
                "turnover_egp": str(plan.turnover_egp),
                "notes": list(plan.notes),
                "target_weights": {k: str(v) for k, v in plan.target_weights.items()},
                "current_weights": {k: str(v) for k, v in plan.current_weights.items()},
                "drift": {
                    symbol: str(plan.target_weights[symbol] - plan.current_weights.get(symbol, Decimal(0)))
                    for symbol in plan.target_weights
                },
                "orders": [
                    {
                        "symbol": o.symbol,
                        "side": o.side.value,
                        "quantity": str(o.quantity),
                        "limit_price": str(o.limit_price),
                        "rationale": o.rationale,
                    }
                    for o in allowed
                ],
                "suppressed": [
                    {"symbol": o.symbol, "side": o.side.value, "reason": reason}
                    for o, reason in suppressed
                ],
                "skipped": [
                    {"symbol": s.symbol, "side": s.side.value, "reason": s.reason}
                    for s in plan.skipped
                ],
            },
        )
        self.bus.publish(
            EventKind.PLAN,
            f"{len(allowed)} order(s) approved, {len(suppressed)} suppressed by regime, "
            f"{len(plan.skipped)} skipped by strategy",
            phase="planning",
            data={"policy_state": plan.policy_state},
        )

    # --------------------------------------------------------------- connection

    async def _ensure_connected(self) -> ThndrExecutor:
        """Build the guarded interface and executor on first use.

        Called only after the regime gate has permitted screen access, which is why
        it is lazy rather than set up in __init__.
        """
        if self._executor is not None:
            return self._executor
        if self._executor_factory is not None:
            self._executor = self._executor_factory(self.bus)
            self.bus.publish(EventKind.LIFECYCLE, "browser bridge attached", phase="idle")
            return self._executor

        if getattr(self._computer, "_initialized", False) is False:
            await self._computer.run()

        raw_interface = self._computer.interface
        self._interface = GuardedInterface(
            raw_interface,
            bus=self.bus,
            guard=self.guard,
            # The app's own published labels are the strongest evidence the guard
            # can get -- stronger than anything inferred from pixels, and immune
            # to theming and font rendering. Leaving this unwired would silently
            # reduce the guard to OCR plus an accent-colour check at runtime,
            # which is exactly the configuration least likely to be calibrated.
            accessibility_tree_provider=getattr(
                raw_interface, "get_accessibility_tree", None
            ),
            mode=self.config.mode,
            # None when uncalibrated, which is what makes a prepare-rung guard
            # refuse to start rather than run without the check it depends on.
            submit_fence=self.config.ui.submit_fence(),
        )

        agent = None
        if self._agent_factory is not None:
            # The factory receives the *guarded* computer, never the real one.
            agent = self._agent_factory(GuardedComputer(self._computer, self._interface))

        self._executor = ThndrExecutor(
            interface=self._interface,
            bus=self.bus,
            ui=self.config.ui,
            agent=agent,
            vision_completion=self._vision_completion,
        )
        self.bus.publish(EventKind.LIFECYCLE, "guarded interface attached", phase="idle")
        return self._executor


def _aliases(policy: AllocationPolicy) -> dict[str, str]:
    """Company-name aliases so headlines naming a company resolve to its ticker."""
    aliases: dict[str, str] = {}
    for instrument in policy.universe:
        aliases[instrument.name] = instrument.symbol
        # First two words are usually the recognisable short name, e.g.
        # "Talaat Moustafa" for "Talaat Moustafa Group Holding".
        words = instrument.name.split()
        if len(words) >= 2:
            aliases[" ".join(words[:2])] = instrument.symbol
    return aliases

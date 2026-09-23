"""Execution layer: the only code that talks to the Thndr UI.

How the guard survives an LLM-driven agent
------------------------------------------
`cua_agent.ComputerAgent` does not click through us -- it takes a `Computer` and
reaches for `computer.interface` itself (see
`libs/python/agent/cua_agent/computers/cua.py`, which does
`self.interface = self.cua_computer.interface` and then calls `left_click` on it).
Hand it the real `Computer` and every safety property in this package is bypassed
by a model deciding where to tap.

So we never hand it the real one. `GuardedComputer` delegates everything to the
real `Computer` except `.interface`, which returns the `GuardedInterface`. The
agent, the handler, and anything else downstream get the guarded object because
it is the only object reachable from what they were given. Model-chosen clicks go
through the same kill-switch and demo-assertion gates as our own scripted ones.

Calibration honesty
-------------------
The anchors in `ThndrUiMap` are placeholders. Nobody -- including a language model
-- can know the current pixel geometry or exact label text of a third-party
trading app from memory, and guessing here would produce code that looks
authoritative and clicks the wrong button. They must be calibrated against real
screenshots before the bot is armed, and `calibration_complete` gates arming so
an uncalibrated map cannot trade. Semantic navigation via `ComputerAgent` is the
default path precisely because it degrades more gracefully than stale
coordinates: a model asked to "open the portfolio tab" adapts to a redesign,
whereas a hard-coded point silently clicks whatever moved into that spot.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence

from ..bus import EventKind, StateBus
from ..safety.demo_guard import ActionRisk, DemoModeViolation, DemoState
from ..safety.guarded_interface import GuardedInterface
from ..types import Portfolio, Position, ProposedOrder, Side

logger = logging.getLogger(__name__)


class ExecutionError(RuntimeError):
    """A UI flow did not reach the state it was supposed to reach."""


class PortfolioReadError(ExecutionError):
    """The portfolio could not be read, or failed validation."""


# --------------------------------------------------------------------------- #
# Guarded Computer
# --------------------------------------------------------------------------- #


class GuardedComputer:
    """A `Computer` look-alike whose `.interface` is the guarded one.

    Pass this wherever cua expects a `Computer` -- `ComputerAgent(tools=[...])`
    included -- so there is no reachable path to the raw interface.
    """

    def __init__(self, computer: Any, guarded_interface: GuardedInterface) -> None:
        self._computer = computer
        self._guarded = guarded_interface

    @property
    def interface(self) -> GuardedInterface:
        return self._guarded

    @property
    def _initialized(self) -> bool:
        return getattr(self._computer, "_initialized", True)

    async def run(self) -> Any:
        return await self._computer.run()

    def __getattr__(self, name: str) -> Any:
        # Deliberately narrow: anything not named here falls through to the real
        # Computer, but `interface` is a property on this class and so always wins.
        if name == "interface":  # pragma: no cover - property takes precedence
            return self._guarded
        return getattr(self._computer, name)


# --------------------------------------------------------------------------- #
# UI map
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ThndrUiMap:
    """Labels and prompts used to navigate. Calibrate before arming.

    Text anchors are preferred over coordinates throughout: they survive a layout
    change, and when they stop matching the bot fails loudly instead of clicking
    an unknown control.
    """

    account_switcher_label: str = "Account"
    simulator_option_label: str = "Simulator"
    portfolio_tab_label: str = "Portfolio"
    search_label: str = "Search"
    buy_button_label: str = "Buy"
    sell_button_label: str = "Sell"
    quantity_field_label: str = "Quantity"
    limit_price_field_label: str = "Limit price"
    review_button_label: str = "Review"
    confirm_button_label: str = "Confirm"
    order_placed_label: str = "Order placed"
    #: Set to True only after a human has verified every label above against the
    #: live app. `ThndrExecutor` refuses to submit orders while this is False.
    calibration_complete: bool = False


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #


@dataclass
class ThndrExecutor:
    """High-level Thndr operations, all routed through the guard."""

    interface: GuardedInterface
    bus: StateBus
    ui: ThndrUiMap = field(default_factory=ThndrUiMap)
    #: Optional `cua_agent.ComputerAgent`, already constructed over a
    #: `GuardedComputer`. When absent, only the read paths work and order
    #: submission raises -- better than clicking blind.
    agent: Optional[Any] = None
    #: Cap on model turns per UI task, so a confused agent cannot flail forever.
    max_turns_per_task: int = 12

    # --------------------------------------------------------------- demo gating

    async def ensure_simulator(self) -> None:
        """Confirm the simulator is active, switching to it if needed.

        Switching is attempted at most once. If the screen still is not confirmed
        demo afterwards, this raises: repeatedly poking an account switcher we do
        not understand is how a bot ends up on the live tab.
        """
        verdict = await self.interface.assert_demo_now()
        if verdict.state is DemoState.CONFIRMED_DEMO:
            self.bus.publish(
                EventKind.GUARD, "simulator already active", phase="asserting_demo",
                data=verdict.to_json(),
            )
            return

        if verdict.state is DemoState.CONFIRMED_LIVE:
            # Do not try to "fix" this by clicking. We are looking at real money;
            # the correct action is to stop and tell a human.
            raise DemoModeViolation(
                "live account is on screen; refusing to interact at all", verdict
            )

        self.bus.publish(
            EventKind.GUARD,
            f"simulator not confirmed ({verdict.detail}); attempting one switch",
            phase="asserting_demo",
            data=verdict.to_json(),
        )
        await self._agent_task(
            f"Open the account switcher (labelled something like "
            f"'{self.ui.account_switcher_label}') and select the "
            f"'{self.ui.simulator_option_label}' / virtual / demo account. "
            f"Do not place any order. Do not confirm anything. Stop once the "
            f"simulator badge is visible."
        )

        verdict = await self.interface.assert_demo_now()
        if verdict.state is not DemoState.CONFIRMED_DEMO:
            raise DemoModeViolation(
                f"could not confirm the simulator after switching: {verdict.detail}",
                verdict,
            )

    # ------------------------------------------------------------ portfolio read

    async def read_portfolio(self) -> Portfolio:
        """Read holdings off the portfolio screen.

        Read-only throughout, so it runs before any assertion has been made --
        which it has to, since we need the screen open to assert anything about it.
        """
        await self._agent_task(
            f"Open the '{self.ui.portfolio_tab_label}' screen so that all holdings "
            f"and the cash balance are visible. Scroll to the top. "
            f"Do not tap Buy, Sell, or Confirm. Do not change the account."
        )
        screenshot = await self.interface.screenshot()
        payload = await self._extract_portfolio(screenshot)
        verdict = await self.interface.assert_demo_now()
        portfolio = _parse_portfolio(payload, demo_confirmed=verdict.state.may_click)

        self.bus.put(
            "portfolio",
            {
                "as_of": portfolio.as_of.isoformat(),
                "total_value": str(portfolio.total_value),
                "cash_egp": str(portfolio.cash_egp),
                "unsettled_cash_egp": str(portfolio.unsettled_cash_egp),
                "demo_confirmed": portfolio.demo_confirmed,
                "positions": [
                    {
                        "symbol": p.symbol,
                        "quantity": str(p.quantity),
                        "market_value": str(p.market_value),
                    }
                    for p in portfolio.positions.values()
                ],
            },
        )
        return portfolio

    async def _extract_portfolio(self, screenshot: bytes) -> Mapping[str, Any]:
        """Turn the portfolio screen into structured data via the vision model."""
        if self.agent is None:
            raise PortfolioReadError(
                "no ComputerAgent configured; cannot read the portfolio screen"
            )
        reply = await self._agent_task(
            "Read the portfolio screen currently visible and return ONLY a JSON "
            "object, no prose, of the form: "
            '{"cash_egp": "0.00", "unsettled_cash_egp": "0.00", "positions": '
            '[{"symbol": "COMI.CA", "quantity": "100", "market_value": "8500.00"}]}. '
            "Use the exchange ticker with a .CA suffix. Market value is in EGP. "
            "If a number is not legible, omit that position rather than guessing.",
            read_only=True,
        )
        try:
            return json.loads(_extract_json_object(reply))
        except Exception as exc:  # noqa: BLE001
            raise PortfolioReadError(f"portfolio extraction did not parse: {exc}") from exc

    # ------------------------------------------------------------------- ordering

    async def submit_order(self, order: ProposedOrder) -> None:
        """Place one order inside an order-critical block.

        The block re-asserts demo mode on entry, elevates every primitive inside
        it to the strictest tier, and re-verifies on exit. If the account switched
        mid-flow, `_post_verify` halts the bot rather than reporting success.
        """
        if not self.ui.calibration_complete:
            raise ExecutionError(
                "ThndrUiMap.calibration_complete is False: calibrate the UI labels "
                "against real screenshots before allowing order submission"
            )
        if self.agent is None:
            raise ExecutionError("no ComputerAgent configured; refusing to click blind")

        side_label = (
            self.ui.buy_button_label if order.side is Side.BUY else self.ui.sell_button_label
        )
        description = (
            f"{order.side.value.upper()} {order.quantity} {order.symbol} "
            f"limit {order.limit_price}"
        )

        async with self.interface.order_critical(description):
            await self._agent_task(
                f"In the Thndr simulator, place a LIMIT {order.side.value.upper()} order:\n"
                f"  symbol: {order.symbol}\n"
                f"  quantity: {order.quantity}\n"
                f"  limit price: {order.limit_price} EGP\n"
                f"Steps: search for {order.symbol}, open it, tap "
                f"'{side_label}', set order type to LIMIT, enter the quantity in "
                f"'{self.ui.quantity_field_label}' and the price in "
                f"'{self.ui.limit_price_field_label}', then "
                f"'{self.ui.review_button_label}' and "
                f"'{self.ui.confirm_button_label}'.\n"
                f"Hard rules: never change the account; never exceed the stated "
                f"quantity; if the quantity or price field will not accept the exact "
                f"value, abort and report instead of substituting a different value."
            )
            confirmed = await self._confirm_order_placed(order)

        if not confirmed:
            raise ExecutionError(
                f"could not confirm the order was accepted: {description}. "
                f"Treat as UNKNOWN and reconcile against the account before retrying."
            )
        self.bus.publish(
            EventKind.ORDER,
            f"submitted {description}",
            phase="executing",
            data={
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": str(order.quantity),
                "limit_price": str(order.limit_price),
                "rationale": order.rationale,
            },
        )

    async def _confirm_order_placed(self, order: ProposedOrder) -> bool:
        """Verify the confirmation state rather than assuming the tap worked.

        A failure here is reported as UNKNOWN, never as "did not happen": the order
        may well be live. Retrying on an unverified submission is how duplicates
        get created.
        """
        reply = await self._agent_task(
            f"Look at the screen now. Answer with exactly one word: "
            f"PLACED if a confirmation for a {order.side.value} order of "
            f"{order.symbol} is visible (for example '{self.ui.order_placed_label}' "
            f"or an entry in the orders list), REJECTED if an error is shown, or "
            f"UNKNOWN if you cannot tell. Do not tap anything.",
            read_only=True,
        )
        answer = (reply or "").strip().upper()
        self.bus.publish(
            EventKind.ORDER,
            f"post-submit check for {order.symbol}: {answer[:40] or 'no answer'}",
            phase="executing",
        )
        return answer.startswith("PLACED")

    # ------------------------------------------------------------------ agent I/O

    async def _agent_task(self, instruction: str, *, read_only: bool = False) -> str:
        """Run one bounded ComputerAgent task and return its final text.

        Every click the agent makes inside here still passes through
        `GuardedInterface`, because the agent only ever saw `GuardedComputer`.
        """
        if self.agent is None:
            raise ExecutionError("no ComputerAgent configured")

        self.bus.publish(
            EventKind.LIFECYCLE,
            f"agent task: {instruction.splitlines()[0][:120]}",
            phase="executing",
            data={"read_only": read_only},
        )

        history: list[dict[str, Any]] = [{"role": "user", "content": instruction}]
        final_text: list[str] = []
        turns = 0

        async for result in self.agent.run(history, stream=False):
            turns += 1
            for item in result.get("output", []):
                if item.get("type") == "message":
                    for block in item.get("content", []):
                        text = block.get("text", "")
                        if text:
                            final_text.append(text)
                elif item.get("type") == "computer_call":
                    # Already journalled by the guard; kept here for the phase label.
                    logger.debug("agent computer_call: %s", item.get("action"))
            if turns >= self.max_turns_per_task:
                self.bus.publish(
                    EventKind.ERROR,
                    f"agent task hit the {self.max_turns_per_task}-turn cap",
                    phase="executing",
                )
                break

        return "\n".join(final_text)


# --------------------------------------------------------------------------- #
# Parsing and validation
# --------------------------------------------------------------------------- #


def _extract_json_object(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text


def _parse_portfolio(payload: Mapping[str, Any], *, demo_confirmed: bool) -> Portfolio:
    """Validate an extracted portfolio before anything is allowed to size against it.

    A vision model misreading a decimal point is not hypothetical, and a portfolio
    read is the denominator of every weight in the plan. So the read is validated
    rather than trusted: negatives, non-numerics and malformed tickers are hard
    errors, not values to clamp.
    """
    positions: dict[str, Position] = {}
    for entry in payload.get("positions", []) or []:
        if not isinstance(entry, Mapping):
            raise PortfolioReadError(f"position entry is not an object: {entry!r}")
        symbol = str(entry.get("symbol", "")).strip().upper()
        if not re.fullmatch(r"[A-Z]{3,5}\.CA", symbol):
            raise PortfolioReadError(f"implausible ticker in portfolio read: {symbol!r}")
        quantity = _decimal(entry.get("quantity"), f"{symbol} quantity")
        value = _decimal(entry.get("market_value"), f"{symbol} market_value")
        if quantity < 0 or value < 0:
            raise PortfolioReadError(f"negative quantity or value for {symbol}")
        if symbol in positions:
            raise PortfolioReadError(f"duplicate position for {symbol}")
        positions[symbol] = Position(symbol, quantity, value)

    cash = _decimal(payload.get("cash_egp", "0"), "cash_egp")
    unsettled = _decimal(payload.get("unsettled_cash_egp", "0"), "unsettled_cash_egp")
    if cash < 0 or unsettled < 0:
        raise PortfolioReadError("negative cash balance in portfolio read")
    if unsettled > cash:
        raise PortfolioReadError(
            f"unsettled cash {unsettled} exceeds total cash {cash}; read is inconsistent"
        )
    if not positions and cash == 0:
        raise PortfolioReadError("portfolio read produced no positions and no cash")

    return Portfolio(
        as_of=datetime.now(timezone.utc),
        positions=positions,
        cash_egp=cash,
        unsettled_cash_egp=unsettled,
        demo_confirmed=demo_confirmed,
    )


def _decimal(value: Any, label: str) -> Decimal:
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value).replace(",", "").replace("EGP", "").strip() or "0")
    except (InvalidOperation, ValueError) as exc:
        raise PortfolioReadError(f"{label} is not a number: {value!r}") from exc

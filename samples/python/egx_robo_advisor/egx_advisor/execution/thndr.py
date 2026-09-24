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

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from ..bus import EventKind, StateBus
from ..safety.demo_guard import DemoModeViolation, DemoState
from ..safety.guarded_interface import GuardedInterface
from ..types import Portfolio, Position, ProposedOrder, Side

if TYPE_CHECKING:
    from ..safety.guarded_interface import SubmitFence

logger = logging.getLogger(__name__)


def default_vision_model() -> str:
    """The litellm model that reads the portfolio screenshot.

    EGX_VISION_MODEL if set, else the chat model, else a Gemini default. Read
    at call time rather than import time, so a value in `.env` takes effect.
    """
    return (
        os.environ.get("EGX_VISION_MODEL")
        or os.environ.get("EGX_CHAT_MODEL")
        or "gemini/gemini-2.5-pro"
    )


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


@dataclass(frozen=True)
class ThndrUiMap:
    """Labels and prompts used to navigate. Calibrate before arming.

    Text anchors are preferred over coordinates throughout: they survive a layout
    change, and when they stop matching the bot fails loudly instead of clicking
    an unknown control.

    These defaults are placeholders shaped like a mobile app's chrome. A desktop
    or web interface labels things differently -- often more verbosely -- so
    calibrate against whichever surface you actually drive. Getting this wrong
    does not silently mis-click: navigation simply fails to find its anchor, and
    `calibration_complete` keeps order submission blocked until a human has
    checked each label against real screenshots.
    """

    #: The web app's URL. Empty means "assume it is already open", which is the
    #: safer default: navigating implies typing into an address bar, and that is
    #: an order-critical primitive on a desktop the bot does not own.
    web_url: str = ""
    #: Suffix the market-data provider uses that the broker's UI does not.
    #: Thndr X lists bare EGX tickers ("COMI", "ABUK") while Yahoo and this
    #: codebase carry ".CA". Typing "COMI.CA" into the broker's search finds
    #: nothing, so every symbol crossing into the UI is translated.
    broker_symbol_suffix: str = ".CA"
    #: Exceptions where the broker's ticker is not just the stripped symbol.
    symbol_overrides: Mapping[str, str] = field(default_factory=dict)
    account_switcher_label: str = "Account"
    simulator_option_label: str = "Simulator"
    portfolio_tab_label: str = "Portfolio"
    #: Thndr X shows holdings under a "Positions" tab, not "Portfolio".
    positions_tab_label: str = "Positions"
    orders_tab_label: str = "Orders"
    #: Text the app shows when the session is closed. Cross-checked against our
    #: own calendar: if they disagree, trust the exchange and stand down.
    market_closed_label: str = "Market Closed"
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

    #: Screen rectangle of the broker's submit button, as `[left, top, right,
    #: bottom]`. Required by `LIVE_PREPARE_ONLY`, which fills a real ticket it
    #: must never commit: the guard refuses any click inside it. Measure it from
    #: a screenshot with the ticket open, and leave a margin -- a fence that is
    #: exactly the button's bounding box fails on the first layout nudge.
    submit_button_rect: tuple[int, int, int, int] | None = None

    def submit_fence(self) -> "SubmitFence | None":
        """The no-click rectangle, or None if it was never measured.

        None is not "no fence needed": `LIVE_PREPARE_ONLY` refuses to start
        without one. Returning None here is how that refusal gets triggered,
        rather than quietly substituting a default rectangle that protects
        nothing in particular.
        """
        from ..safety.guarded_interface import SubmitFence

        if self.submit_button_rect is None:
            return None
        left, top, right, bottom = self.submit_button_rect
        return SubmitFence(
            left=left, top=top, right=right, bottom=bottom,
            label=f"{self.confirm_button_label} button",
        )

    def broker_symbol(self, canonical: str) -> str:
        """Canonical symbol -> what the broker's UI calls it."""
        override = self.symbol_overrides.get(canonical)
        if override:
            return override
        suffix = self.broker_symbol_suffix
        return canonical[: -len(suffix)] if suffix and canonical.endswith(suffix) else canonical

    def canonical_symbol(self, broker: str) -> str:
        """The reverse, for reading a portfolio back off the screen."""
        for canonical, mapped in self.symbol_overrides.items():
            if mapped.upper() == broker.upper():
                return canonical
        broker = broker.strip().upper()
        suffix = self.broker_symbol_suffix
        return broker if not suffix or broker.endswith(suffix) else f"{broker}{suffix}"

    @classmethod
    def from_toml(cls, path: str | Path) -> "ThndrUiMap":
        """Load a calibrated map from TOML.

        Calibration is data, not code: the labels belong in a file a human edits
        after looking at their own screen, not in a Python literal that has to be
        patched. `calibrate_ui.py` prints what the live app actually exposes.
        """
        import tomllib

        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        section = raw.get("ui", raw)
        known = {f.name for f in fields(cls)}
        unknown = set(section) - known
        if unknown:
            raise ValueError(
                f"{path}: unknown key(s) {sorted(unknown)}; expected {sorted(known)}"
            )
        rect = section.get("submit_button_rect")
        if rect is not None:
            if len(rect) != 4:
                raise ValueError(
                    f"{path}: submit_button_rect must be [left, top, right, bottom], "
                    f"got {rect!r}"
                )
            section["submit_button_rect"] = tuple(int(v) for v in rect)
        return cls(**section)


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
    #: litellm model that reads the portfolio off a screenshot. A plain vision
    #: call, not a computer-use agent: reading must not come with the ability
    #: to click. Read when the executor is built, so `.env` has been loaded.
    vision_model: str = field(default_factory=lambda: default_vision_model())
    #: Injected in tests; `litellm.completion` otherwise.
    vision_completion: Optional[Callable[..., Any]] = None
    #: Seconds before a vision call is abandoned, so a hung request cannot
    #: freeze the loop.
    vision_timeout: float = 90.0

    # --------------------------------------------------------------- demo gating

    async def ensure_simulator(self) -> None:
        """Confirm the account on screen is the one this mode expects.

        In `SIMULATOR_ONLY` that means the simulator, switching to it at most
        once. Switching is attempted only once because repeatedly poking an
        account switcher we do not understand is how a bot ends up on the live
        tab.

        In `LIVE_READ_ONLY` a real account is what we came to look at, so no
        switch is attempted and no state is fatal -- the verdict is recorded so
        the dashboard can show which account is being observed. Nothing this
        mode can reach is capable of placing an order: the guard refuses every
        order-critical primitive, and `submit_order` below declines outright.
        """
        if not self.interface.mode.permits_order_tickets:
            verdict = await self.interface.assert_demo_now()
            self.bus.publish(
                EventKind.GUARD,
                f"{self.interface.mode.banner}: observing "
                f"{verdict.state.value} ({verdict.detail})",
                phase="asserting_demo",
                data=verdict.to_json(),
            )
            return

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

        On a rung that may open tickets, the agent first navigates to the
        holdings. On `LIVE_READ_ONLY` nothing is clicked -- the guard would
        refuse it anyway -- so the screen is read exactly as the operator left
        it, and the operator is told which tab to leave open.
        """
        if self.interface.mode.permits_order_tickets:
            await self._agent_task(
                f"Open the '{self.ui.portfolio_tab_label}' screen so that all holdings "
                f"and the cash balance are visible. Scroll to the top. "
                f"Do not tap Buy, Sell, or Confirm. Do not change the account."
            )
        else:
            self.bus.publish(
                EventKind.LIFECYCLE,
                f"reading the screen as it is (no clicks on this rung); keep Thndr X "
                f"in front with the '{self.ui.positions_tab_label}' tab open",
                phase="reading_portfolio",
            )
        screenshot = await self.interface.screenshot()
        payload = await self._extract_portfolio(screenshot)
        cash_visible = payload.get("cash_egp") not in (None, "")
        verdict = await self.interface.assert_demo_now()
        portfolio = _parse_portfolio(payload, demo_confirmed=verdict.state.may_click)
        publish_portfolio(self.bus, portfolio, cash_visible=cash_visible)
        return portfolio

    async def _extract_portfolio(self, screenshot: bytes) -> Mapping[str, Any]:
        """Turn the portfolio screen into structured data with a vision model.

        The screenshot is attached to the request. The earlier version asked a
        computer-use agent in text alone, so the model never saw the screen,
        and the prompt's example values could come back as a well-formed,
        entirely invented portfolio. The prompt now carries no numbers to echo.
        """
        if not screenshot:
            raise PortfolioReadError("empty screenshot; nothing to read")
        encoded = base64.b64encode(screenshot).decode("ascii")
        return await extract_positions(
            ui=self.ui,
            bus=self.bus,
            model=self.vision_model,
            completion=self.vision_completion,
            timeout=self.vision_timeout,
            prompt=positions_prompt(self.ui, source="a screenshot"),
            attachment={
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{encoded}"},
            },
            wrong_screen=(
                "the window in front is not the Thndr X positions table; bring Thndr X "
                f"to the front with the '{self.ui.positions_tab_label}' tab open"
            ),
        )

    # ------------------------------------------------------------------- ordering

    async def prepare_order(self, order: ProposedOrder) -> None:
        """Fill an order ticket on screen and stop, leaving submit to the operator.

        The counterpart to `submit_order` for an operator who has no simulator
        account. The bot does the tedious, error-prone part -- finding the
        symbol, choosing the side, setting LIMIT, typing a quantity and a price
        that match the plan exactly -- and then takes its hands off. The last
        action is a person looking at a filled ticket on their own screen and
        pressing the broker's own button, or discarding it.

        What stops this method becoming `submit_order` by accident is not this
        docstring. Inside the block the guard refuses Enter, refuses a newline in
        typed text, and refuses any click inside the calibrated fence around the
        submit button, so the instruction below is a description of what the
        proxy will permit rather than a request it is trusted to honour.

        Nothing here reports success: a prepared ticket is not an order, and
        publishing one as though it were would put a fill in the journal that
        never happened.
        """
        if not self.interface.mode.permits_order_tickets:
            raise ExecutionError(
                f"{self.interface.mode.banner}: this mode observes an account and "
                f"never opens a ticket on it. Refusing to prepare "
                f"{order.side.value} {order.quantity} {order.symbol}."
            )
        if not self.ui.calibration_complete:
            raise ExecutionError(
                "ThndrUiMap.calibration_complete is False: calibrate the UI labels "
                "against real screenshots before allowing the bot near a ticket"
            )
        if self.agent is None:
            raise ExecutionError("no ComputerAgent configured; refusing to click blind")

        side_label = (
            self.ui.buy_button_label if order.side is Side.BUY else self.ui.sell_button_label
        )
        ui_symbol = self.ui.broker_symbol(order.symbol)
        description = (
            f"{order.side.value.upper()} {order.quantity} {order.symbol} "
            f"limit {order.limit_price}"
        )

        async with self.interface.order_critical(f"PREPARE {description}"):
            await self._agent_task(
                f"Fill in a LIMIT {order.side.value.upper()} ticket and then STOP:\n"
                f"  symbol: {ui_symbol}\n"
                f"  quantity: {order.quantity}\n"
                f"  limit price: {order.limit_price} EGP\n"
                f"Steps: search for {ui_symbol}, open it, tap "
                f"'{side_label}', set order type to LIMIT, enter the quantity in "
                f"'{self.ui.quantity_field_label}' and the price in "
                f"'{self.ui.limit_price_field_label}'.\n"
                f"Then STOP. Do NOT tap '{self.ui.review_button_label}', do NOT tap "
                f"'{self.ui.confirm_button_label}', and do NOT press Enter. A human "
                f"submits this ticket, not you. Leave it filled and on screen.\n"
                f"Hard rules: never change the account; never exceed the stated "
                f"quantity; if the quantity or price field will not accept the exact "
                f"value, abort and report instead of substituting a different value."
            )

        self.bus.publish(
            EventKind.ORDER,
            f"TICKET READY, NOT SUBMITTED - {description}",
            phase="awaiting_operator",
            data={
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": str(order.quantity),
                "limit_price": str(order.limit_price),
                "rationale": order.rationale,
                "submitted": False,
                "awaiting": "operator presses submit on the broker's own screen",
            },
        )

    async def submit_order(self, order: ProposedOrder) -> None:
        """Place one order inside an order-critical block.

        The block re-asserts demo mode on entry, elevates every primitive inside
        it to the strictest tier, and re-verifies on exit. If the account switched
        mid-flow, `_post_verify` halts the bot rather than reporting success.
        """
        if not self.interface.mode.permits_orders:
            raise ExecutionError(
                f"{self.interface.mode.banner}: this mode observes an account and "
                f"never trades on it. Refusing to submit "
                f"{order.side.value} {order.quantity} {order.symbol}."
            )
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
        ui_symbol = self.ui.broker_symbol(order.symbol)
        description = (
            f"{order.side.value.upper()} {order.quantity} {order.symbol} "
            f"limit {order.limit_price}"
        )

        async with self.interface.order_critical(description):
            await self._agent_task(
                f"In the Thndr simulator, place a LIMIT {order.side.value.upper()} order:\n"
                f"  symbol: {ui_symbol}\n"
                f"  quantity: {order.quantity}\n"
                f"  limit price: {order.limit_price} EGP\n"
                f"Steps: search for {ui_symbol}, open it, tap "
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
# Portfolio reading, shared by the screen and the in-app browser
# --------------------------------------------------------------------------- #


def positions_prompt(ui: "ThndrUiMap", *, source: str) -> str:
    """The extraction prompt. It carries no numbers, so none can be echoed back."""
    return (
        f"This is {source} of the Thndr X trading app. Read the "
        f"'{ui.positions_tab_label}' table and return ONLY a JSON object, "
        "no prose, with exactly these keys:\n"
        '  "positions": a list of objects with "symbol", "quantity", "market_value"\n'
        '  "cash_egp": the available cash balance in EGP, or null\n'
        '  "unsettled_cash_egp": unsettled cash in EGP, or null\n'
        "Rules: use each ticker exactly as the table shows it. quantity is the Qty "
        "column. market_value is the market value column in EGP; the header may be "
        "abbreviated (Thndr X shows 'Mkt. Val...'). Never use AvgCost, Weight or "
        "P/L for either. Copy digits as shown, without thousands separators. If "
        "the cash balance is not shown, set cash_egp to null -- do not "
        "guess and do not write zero. If a number is not legible, omit that position. "
        "If this is not the Thndr X positions table, return {\"positions\": [], "
        '"cash_egp": null, "unsettled_cash_egp": null, "not_positions_screen": true}.'
    )


async def extract_positions(
    *,
    ui: "ThndrUiMap",
    bus: StateBus,
    model: str,
    completion: Optional[Callable[..., Any]],
    timeout: float,
    prompt: str,
    attachment: Optional[Mapping[str, Any]] = None,
    wrong_screen: str,
) -> dict[str, Any]:
    """One model call that turns the positions view into JSON, validated shape-first.

    `attachment` is an extra content part -- the screenshot -- or None when the
    prompt already carries the page text.
    """
    if completion is None:
        try:
            from litellm import completion as litellm_completion
        except ImportError as exc:
            raise PortfolioReadError(
                f"litellm is not installed, so the portfolio cannot be read: {exc}"
            ) from exc
        completion = litellm_completion

    content: list[Mapping[str, Any]] = [{"type": "text", "text": prompt}]
    if attachment is not None:
        content.append(attachment)
    bus.publish(
        EventKind.LIFECYCLE,
        f"reading the positions table with {model}",
        phase="reading_portfolio",
    )
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                completion,
                model=model,
                messages=[{"role": "user", "content": content}],
                timeout=timeout,
            ),
            timeout=timeout + 10,
        )
        reply = response["choices"][0]["message"]["content"] or ""
    except asyncio.TimeoutError as exc:
        raise PortfolioReadError(f"the model did not answer within {timeout:.0f}s") from exc
    except Exception as exc:  # noqa: BLE001 - provider errors come in many types
        raise PortfolioReadError(f"model call failed: {exc}") from exc

    try:
        payload = json.loads(_extract_json_object(reply))
    except Exception as exc:  # noqa: BLE001
        raise PortfolioReadError(f"portfolio extraction did not parse: {exc}") from exc
    if not isinstance(payload, dict):
        raise PortfolioReadError("portfolio extraction was not a JSON object")
    if payload.get("not_positions_screen"):
        raise PortfolioReadError(wrong_screen)

    # Translate the broker's tickers back to canonical ones before anything
    # sizes against them; the strategy and the price feed both speak ".CA".
    positions = payload.get("positions")
    if isinstance(positions, list):
        for entry in positions:
            if isinstance(entry, dict) and entry.get("symbol"):
                entry["symbol"] = ui.canonical_symbol(str(entry["symbol"]))
    return payload


def publish_portfolio(
    bus: StateBus, portfolio: Portfolio, *, cash_visible: bool, source: str = "screen"
) -> None:
    """Put a validated portfolio on the bus, warning when cash was not shown."""
    if not cash_visible:
        bus.publish(
            EventKind.GUARD,
            "cash balance not visible on screen: the plan treats cash as 0, so "
            "it proposes no buys and the weights leave cash out",
            phase="reading_portfolio",
        )
    bus.put(
        "portfolio",
        {
            "as_of": portfolio.as_of.isoformat(),
            "total_value": str(portfolio.total_value),
            "cash_egp": str(portfolio.cash_egp),
            "unsettled_cash_egp": str(portfolio.unsettled_cash_egp),
            "demo_confirmed": portfolio.demo_confirmed,
            "cash_visible": cash_visible,
            "source": source,
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

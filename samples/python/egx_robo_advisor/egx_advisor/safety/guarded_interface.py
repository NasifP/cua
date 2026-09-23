"""The chokepoint. Every input the bot sends to Thndr passes through here.

The design idea worth arguing for
--------------------------------
Safety checks that live in the *caller* are advisory: the next person to add an
order flow forgets one, and nothing complains. So the guard is not a function the
execution layer is asked to call politely -- it is a proxy that wraps the cua
`Computer.interface` and is the *only* handle the execution layer is ever given.
There is no code path from strategy to screen that skips it, because there is no
other object to call.

Every mutating call therefore passes, in order:

    1. kill switch   -- StateBus.is_halted(), fail-closed
    2. demo assertion -- fresh enough for this action's risk tier
    3. journal        -- the action is published to the bus *before* it happens
    4. the actual call
    5. post-verification (order-critical only) -- did the screen change identity?

Step 3 before step 4 is deliberate: if the click wedges the app or crashes the
process, the dashboard still shows what the bot was reaching for. A log written
after the fact is missing exactly the entry you need.

Step 5 catches the case the pre-check structurally cannot: the account switching
*during* an order flow, between the assertion and the confirm tap. If the screen
comes back live, the bus is halted immediately and permanently.

Risk tiers combine
------------------
A method has a static tier, and `order_critical()` raises the floor for a block.
`left_click` is ordinary navigation when the bot is walking the watchlist, and
order-critical when it is the Buy button -- same primitive, different stakes,
decided by the caller's context rather than by guessing from coordinates.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Mapping, Optional

from ..bus import EventKind, StateBus
from .demo_guard import (
    ActionRisk,
    DemoGuard,
    DemoModeViolation,
    DemoState,
    DemoVerdict,
)
from .modes import ExecutionMode, OrderTicketsForbidden

logger = logging.getLogger(__name__)


class KillSwitchEngaged(RuntimeError):
    """Raised when the operator has halted the bot. Not retried, not swallowed."""


# --------------------------------------------------------------------------- #
# Method classification
# --------------------------------------------------------------------------- #

#: Calls that only observe. They run under the kill switch but need no demo
#: assertion -- the portfolio read has to happen before there is anything to
#: assert about, and observing cannot place an order.
READ_ONLY_METHODS = frozenset(
    {
        "screenshot",
        "get_screenshot",
        "get_screen_size",
        "get_cursor_position",
        "get_accessibility_tree",
        "to_screen_coordinates",
        "to_screenshot_coordinates",
        "copy_to_clipboard",
        "file_exists",
        "directory_exists",
        "list_dir",
        "read_text",
        "read_bytes",
        "get_file_size",
        "get_current_window_id",
        "get_application_windows",
        "get_window_name",
        "get_window_size",
        "get_window_position",
        "get_window_title",
        "window_size",
        "get_desktop_environment",
        "wait_for_ready",
    }
)

#: Pointer and key primitives. Mutating, but on their own they navigate rather
#: than commit. Elevated to order-critical inside an `order_critical()` block.
NAVIGATION_METHODS = frozenset(
    {
        "move_cursor",
        "scroll",
        "scroll_down",
        "scroll_up",
        "drag",
        "drag_to",
        "left_click",
        "right_click",
        "double_click",
        "mouse_down",
        "mouse_up",
        "press_key",
        "hotkey",
        "key_down",
        "key_up",
        "activate_window",
        "maximize_window",
        "minimize_window",
        "set_window_size",
        "set_window_position",
    }
)

#: Always maximum scrutiny. `type_text` is here because typing into an order
#: ticket is how a quantity gets set, and `set_clipboard` because a paste is a
#: typed quantity by another route.
ORDER_CRITICAL_METHODS = frozenset(
    {
        "type_text",
        "set_clipboard",
        "open",
        "launch",
        "run_command",
        "write_text",
        "write_bytes",
        "delete_file",
        "delete_dir",
        "create_dir",
        "close_window",
        "set_wallpaper",
    }
)


def classify(method_name: str) -> ActionRisk:
    """Risk tier for an interface method.

    Anything unrecognised gets the *strictest* tier, not the loosest. When the
    cua SDK grows a new input primitive, the failure mode is "the bot demands a
    fresh two-source assertion" rather than "a new way to click slipped past the
    guard unnoticed". Relax it by adding the name to a table above.
    """
    if method_name in READ_ONLY_METHODS:
        return ActionRisk.READ_ONLY
    if method_name in NAVIGATION_METHODS:
        return ActionRisk.NAVIGATION
    if method_name in ORDER_CRITICAL_METHODS:
        return ActionRisk.ORDER_CRITICAL
    logger.warning(
        "interface method %r is unclassified; treating it as order-critical", method_name
    )
    return ActionRisk.ORDER_CRITICAL


# --------------------------------------------------------------------------- #
# The proxy
# --------------------------------------------------------------------------- #


@dataclass
class GuardStats:
    calls_allowed: int = 0
    calls_blocked: int = 0
    assertions: int = 0
    last_block_reason: str = ""


class GuardedInterface:
    """Safety-wrapped view of `Computer.interface`.

    Hand this to the execution layer and never hand it the raw interface.

    `reassert_every_action=False` reuses a verdict while it is inside the tier's
    freshness window. It is faster and strictly less safe; the default is on.
    """

    def __init__(
        self,
        raw_interface: Any,
        *,
        bus: StateBus,
        guard: Optional[DemoGuard] = None,
        accessibility_tree_provider: Optional[Callable[[], Any]] = None,
        halt_on_live: bool = True,
        reassert_every_action: bool = True,
        mode: ExecutionMode = ExecutionMode.SIMULATOR_ONLY,
    ) -> None:
        self._raw = raw_interface
        self._bus = bus
        self._guard = guard or DemoGuard.default()
        self._tree_provider = accessibility_tree_provider
        self._halt_on_live = halt_on_live
        self._reassert_every_action = reassert_every_action
        self.mode = mode
        self._verdict: Optional[DemoVerdict] = None
        self._elevation: int = 0
        self._lock = asyncio.Lock()
        self.stats = GuardStats()

    # ------------------------------------------------------------------ elevation

    @asynccontextmanager
    async def order_critical(self, description: str) -> AsyncIterator[None]:
        """Raise the risk floor for an order-submission block.

        Re-asserts on entry so the block starts from a verified screen, and
        re-verifies on exit so a mid-flow account switch cannot go unnoticed.
        """
        if not self.mode.permits_order_tickets:
            self.stats.calls_blocked += 1
            self._bus.publish(
                EventKind.GUARD,
                f"BLOCKED ({self.mode.banner}) order block: {description}",
                phase="executing",
                data={"mode": self.mode.value},
            )
            raise OrderTicketsForbidden(
                f"{self.mode.banner}: refusing to open an order block ({description})"
            )
        await self._require_not_halted(f"entering order block: {description}")
        await self._refresh_verdict(ActionRisk.ORDER_CRITICAL, force=True)
        self._assert_permits(ActionRisk.ORDER_CRITICAL, f"order block: {description}")
        self._elevation += 1
        self._bus.publish(
            EventKind.GUARD,
            f"order block OPEN: {description}",
            phase="executing",
            data={"verdict": self._verdict.to_json() if self._verdict else None},
        )
        try:
            yield
        finally:
            self._elevation -= 1
            await self._post_verify(description)

    @property
    def elevated(self) -> bool:
        return self._elevation > 0

    # -------------------------------------------------------------------- proxying

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        target = getattr(self._raw, name)
        if not callable(target):
            return target

        static_risk = classify(name)

        async def guarded(*args: Any, **kwargs: Any) -> Any:
            risk = self._effective_risk(static_risk)
            label = _format_call(name, args, kwargs)

            await self._require_not_halted(label)
            self._require_mode_allows(risk, label)

            if risk is not ActionRisk.READ_ONLY:
                # Fresh by default. Reusing a verdict across calls opens a window
                # in which the account can switch to real money unnoticed -- the
                # user taps the account switcher, a deep link opens the live tab --
                # and the bot keeps clicking on the strength of a stale "yes".
                # A rebalancer issues tens of clicks per session, not thousands,
                # so paying one screenshot per click is the right trade.
                await self._refresh_verdict(risk, force=self._reassert_every_action)
                self._assert_permits(risk, label)
                # Journal intent *before* acting; see the module docstring.
                self._bus.publish(
                    EventKind.UI_ACTION,
                    label,
                    phase="executing",
                    data={
                        "method": name,
                        "risk": risk.value,
                        "args": _safe_args(args, kwargs),
                        "demo_state": self._verdict.state.value if self._verdict else "unknown",
                    },
                )

            result = target(*args, **kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            self.stats.calls_allowed += 1
            return result

        guarded.__name__ = f"guarded_{name}"
        return guarded

    def _effective_risk(self, static_risk: ActionRisk) -> ActionRisk:
        if static_risk is ActionRisk.READ_ONLY:
            # Reading stays read-only even inside an order block; elevating it
            # would make the pre-order screenshot depend on itself.
            return static_risk
        if self.elevated:
            return ActionRisk.ORDER_CRITICAL
        return static_risk

    # ------------------------------------------------------------------ the gates

    async def _require_not_halted(self, label: str) -> None:
        if self._bus.is_halted():
            state = self._bus.control_state()
            self.stats.calls_blocked += 1
            self.stats.last_block_reason = f"halted: {state.reason}"
            self._bus.publish(
                EventKind.GUARD,
                f"BLOCKED (kill switch) before {label}",
                phase="halted",
                data={"reason": state.reason, "actor": state.actor},
            )
            raise KillSwitchEngaged(
                f"kill switch engaged by {state.actor}: {state.reason}"
            )

    def _require_mode_allows(self, risk: ActionRisk, label: str) -> None:
        """Refuse order-critical work in a mode that only observes.

        On `LIVE_READ_ONLY` the bot may look at a real account and navigate it,
        but every primitive capable of composing an order -- typing into a
        field, running a command, anything unrecognised -- is refused here, at
        the proxy. The executor also declines to open an order block, but that
        is the polite half; this is the half a future code path cannot forget.
        """
        if self.mode.permits_order_tickets:
            return
        if risk is not ActionRisk.ORDER_CRITICAL and not self.elevated:
            return
        self.stats.calls_blocked += 1
        self.stats.last_block_reason = f"{self.mode.value} forbids order-critical work"
        self._bus.publish(
            EventKind.GUARD,
            f"BLOCKED ({self.mode.banner}) before {label}",
            phase="executing",
            data={"mode": self.mode.value, "risk": risk.value},
        )
        raise OrderTicketsForbidden(
            f"{self.mode.banner}: refusing {label}. This mode observes a real "
            f"account and never trades on it."
        )

    def _assert_permits(self, risk: ActionRisk, label: str) -> None:
        verdict = self._verdict
        if verdict is None:
            self.stats.calls_blocked += 1
            raise DemoModeViolation(f"no demo assertion available before {label}")

        if self.mode.permits_live_account:
            # On this rung both accounts are acceptable to *look at*, so the
            # verdict's job is to report which one we are on rather than to gate
            # navigation. The gate that matters has already run:
            # `_require_mode_allows` refuses every order-critical primitive
            # before we get here, so nothing reachable from this point can
            # compose an order. Blocking navigation as well would simply stop
            # the bot reading the portfolio it was pointed at.
            return

        permitted, why = verdict.permits(risk)
        if permitted:
            return
        self.stats.calls_blocked += 1
        self.stats.last_block_reason = why
        self._bus.publish(
            EventKind.GUARD,
            f"BLOCKED ({why}) before {label}",
            phase="halted",
            data={"verdict": verdict.to_json(), "risk": risk.value},
        )
        if (
            verdict.state is DemoState.CONFIRMED_LIVE
            and self._halt_on_live
            and not self.mode.permits_live_account
        ):
            # Not a retryable condition in simulator mode. Real money on screen
            # means the bot's model of the world is wrong; stop and get a human.
            # On LIVE_READ_ONLY a real account is expected, so seeing one is not
            # an emergency -- and it is not a licence either: order-critical work
            # is already refused by _require_mode_allows before reaching here.
            self._bus.halt(
                actor="demo_guard",
                reason=f"real-money environment detected before {label}",
            )
        raise DemoModeViolation(f"refusing {label}: {why}", verdict)

    # -------------------------------------------------------------- assertion path

    async def _refresh_verdict(self, risk: ActionRisk, *, force: bool = False) -> DemoVerdict:
        """Re-assert if the cached verdict is too old for `risk`."""
        async with self._lock:
            cached = self._verdict
            if not force and cached is not None:
                permitted, _ = cached.permits(risk)
                if permitted:
                    return cached
                if cached.state is DemoState.CONFIRMED_LIVE:
                    return cached  # never re-roll a live verdict hoping for better

            screenshot = await self._capture()
            tree = await self._collect_tree()
            verdict = self._guard.assert_demo(screenshot, accessibility_tree=tree)
            self._verdict = verdict
            self.stats.assertions += 1
            self._bus.publish(
                EventKind.GUARD,
                f"demo assertion: {verdict.state.value} ({verdict.detail})",
                phase="asserting_demo",
                data=verdict.to_json(),
            )
            self._bus.put("demo_verdict", verdict.to_json())
            return verdict

    async def _capture(self) -> bytes:
        """Screenshot straight off the raw interface, bypassing this proxy.

        Going through `__getattr__` here would recurse: the assertion needs a
        screenshot, and the screenshot would ask for an assertion.
        """
        try:
            result = self._raw.screenshot()
            if asyncio.iscoroutine(result):
                result = await result
            return result or b""
        except Exception as exc:  # noqa: BLE001
            logger.error("screenshot failed during demo assertion: %s", exc)
            self._bus.publish(
                EventKind.ERROR, f"screenshot failed during assertion: {exc}", phase="asserting_demo"
            )
            return b""  # empty -> INDETERMINATE -> no click

    async def _collect_tree(self) -> Optional[Mapping[str, Any]]:
        """Fetch the app's own published labels, when the platform offers them.

        Accepts a sync or async provider because the cua interface exposes
        `get_accessibility_tree()` as a coroutine. A tree that cannot be fetched
        is simply absent: losing this probe withholds corroboration, it never
        grants it.
        """
        if self._tree_provider is None:
            return None
        try:
            tree = self._tree_provider()
            if asyncio.iscoroutine(tree):
                tree = await tree
            return tree if isinstance(tree, Mapping) else None
        except Exception as exc:  # noqa: BLE001
            logger.info("accessibility tree unavailable: %s", exc)
            return None

    async def _post_verify(self, description: str) -> None:
        """Confirm after an order block that the screen is still the simulator."""
        verdict = await self._refresh_verdict(ActionRisk.ORDER_CRITICAL, force=True)
        if verdict.state is DemoState.CONFIRMED_DEMO:
            self._bus.publish(
                EventKind.GUARD,
                f"order block CLOSED, still simulator: {description}",
                phase="executing",
                data=verdict.to_json(),
            )
            return
        message = (
            f"post-order verification FAILED after {description}: "
            f"{verdict.state.value} ({verdict.detail})"
        )
        self._bus.publish(EventKind.ERROR, message, phase="halted", data=verdict.to_json())
        if self._halt_on_live:
            self._bus.halt(actor="demo_guard", reason=message)
        raise DemoModeViolation(message, verdict)

    # ----------------------------------------------------------------- inspection

    async def assert_demo_now(self) -> DemoVerdict:
        """Force a fresh assertion. Used by the agent's pre-flight check."""
        return await self._refresh_verdict(ActionRisk.ORDER_CRITICAL, force=True)

    @property
    def last_verdict(self) -> Optional[DemoVerdict]:
        return self._verdict


def _format_call(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    rendered = ", ".join(
        [repr(a) for a in args] + [f"{k}={v!r}" for k, v in sorted(kwargs.items())]
    )
    return f"{name}({rendered})"


def _safe_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Journal arguments without dumping a screenshot or a secret into the log."""

    def clean(value: Any) -> Any:
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"
        if isinstance(value, str) and len(value) > 120:
            return value[:120] + "..."
        if isinstance(value, (int, float, bool, str)) or value is None:
            return value
        return repr(value)[:120]

    return {
        "positional": [clean(a) for a in args],
        "keyword": {k: clean(v) for k, v in kwargs.items()},
    }

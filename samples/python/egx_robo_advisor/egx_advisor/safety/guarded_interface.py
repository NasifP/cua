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
from .modes import (
    ExecutionMode,
    OrderTicketsForbidden,
    SubmitFenceMissing,
    SubmitForbidden,
)

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


#: Keys that commit a form. `type_text` carrying one of these as an escape is
#: covered separately, by scanning the text itself.
SUBMIT_KEYS = frozenset(
    {"enter", "return", "kp_enter", "kpenter", "numpad_enter", "\n", "\r"}
)

#: Pointer primitives whose first two positional arguments are a screen point.
#: Anything outside this set is not fence-checkable, and `_fence_reject` says so
#: rather than waving it through on the assumption that it cannot click.
POINTER_METHODS = frozenset(
    {
        "left_click",
        "right_click",
        "double_click",
        "move_cursor",
        "mouse_down",
        "mouse_up",
        "drag_to",
    }
)


def _sends_submit_key(name: str, args: tuple, kwargs: Mapping[str, Any]) -> bool:
    """Whether this call would press Enter, by any of the routes that can.

    Covers the key primitives, a hotkey combination that includes Enter, and
    typed text carrying a newline -- `type_text("600\\n")` submits a form just as
    surely as pressing the key, and is the easier one to write by accident.
    """
    if name == "type_text":
        text = kwargs.get("text")
        if text is None and args:
            text = args[0]
        return isinstance(text, str) and ("\n" in text or "\r" in text)

    if name in ("press_key", "key_down", "key_up", "hotkey"):
        candidates = [a for a in args if isinstance(a, str)]
        candidates += [v for v in kwargs.values() if isinstance(v, str)]
        for value in candidates:
            for part in value.replace("-", "+").split("+"):
                if part.strip().lower().replace(" ", "_") in SUBMIT_KEYS:
                    return True
    return False


def _point_of(args: tuple, kwargs: Mapping[str, Any]) -> Optional[tuple[float, float]]:
    """The (x, y) a pointer call targets, or None if it cannot be read."""
    x = kwargs.get("x")
    y = kwargs.get("y")
    if x is None and len(args) >= 1:
        x = args[0]
    if y is None and len(args) >= 2:
        y = args[1]
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return float(x), float(y)
    return None


@dataclass(frozen=True)
class SubmitFence:
    """A rectangle the bot may not click, in screen coordinates.

    This is the broker's submit button and enough margin around it to survive a
    few pixels of layout drift. It is deliberately a dumb rectangle rather than
    anything clever: the operator can measure it from a screenshot, read it back
    off the config file, and check it themselves, which is not true of a
    heuristic.
    """

    left: int
    top: int
    right: int
    bottom: int
    label: str = "submit button"

    def __post_init__(self) -> None:
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError(
                f"submit fence is empty or inverted: {self!r}. An empty fence "
                f"blocks nothing while looking like a protection."
            )

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x <= self.right and self.top <= y <= self.bottom


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
        submit_fence: Optional[SubmitFence] = None,
    ) -> None:
        self._raw = raw_interface
        self._bus = bus
        self._guard = guard or DemoGuard.default()
        self._tree_provider = accessibility_tree_provider
        self._halt_on_live = halt_on_live
        self._reassert_every_action = reassert_every_action
        self.mode = mode
        if mode.requires_submit_fence and submit_fence is None:
            raise SubmitFenceMissing(
                f"{mode.banner} fills a real order ticket it must never submit, "
                f"so it needs the no-click rectangle around the broker's submit "
                f"button. Calibrate it (see calibrate_ui.py) and pass "
                f"submit_fence=. Refusing to start without it: the mode's whole "
                f"claim rests on that check existing."
            )
        self._submit_fence = submit_fence
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
        opened_as = self._verdict.state if self._verdict else DemoState.INDETERMINATE
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
            await self._post_verify(description, opened_as)

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
            self._require_not_a_submit(name, args, kwargs, label)

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

    def _require_not_a_submit(
        self, name: str, args: tuple, kwargs: Mapping[str, Any], label: str
    ) -> None:
        """Refuse the two routes that commit a ticket without pressing "Buy".

        Only runs where the mode may fill a ticket but never submit it. On the
        simulator there is nothing at stake, and on the read-only rung no ticket
        can be open in the first place.

        Scoped to order blocks on purpose. Enter is an ordinary key while the bot
        is navigating a watchlist or dismissing a dialog; it is a submit only
        once a ticket is open, and a guard that refused it everywhere would be
        switched off for crying wolf.
        """
        if self.mode.permits_orders or not self.mode.permits_order_tickets:
            return
        if not self.elevated:
            return

        if _sends_submit_key(name, args, kwargs):
            self._reject_submit(f"{label}: Enter commits the ticket", label)

        fence = self._submit_fence
        if fence is not None and name in POINTER_METHODS:
            point = _point_of(args, kwargs)
            if point is None:
                # A pointer call whose target we cannot read is a pointer call we
                # cannot fence. Refusing is the only answer that keeps the
                # invariant true; waving it through would quietly make the fence
                # optional for anything the SDK changes the signature of.
                self._reject_submit(
                    f"{label}: cannot read the click target, so it cannot be "
                    f"checked against the {fence.label} fence",
                    label,
                )
            elif fence.contains(*point):
                self._reject_submit(
                    f"{label}: inside the {fence.label} fence "
                    f"({fence.left},{fence.top})-({fence.right},{fence.bottom})",
                    label,
                )

    def _reject_submit(self, why: str, label: str) -> None:
        self.stats.calls_blocked += 1
        self.stats.last_block_reason = why
        self._bus.publish(
            EventKind.GUARD,
            f"BLOCKED ({self.mode.banner}) {why}",
            phase="executing",
            data={"mode": self.mode.value, "call": label},
        )
        raise SubmitForbidden(f"{self.mode.banner}: {why}")

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
        """Refuse every input in a mode that only observes.

        On `LIVE_READ_ONLY` the bot looks at a real account and does nothing
        else: screenshots and other reads pass, and every click, key, hotkey,
        drag, scroll and typed character is refused here, at the proxy. The
        operator puts the screen where it needs to be.

        This used to refuse only order-critical calls and let "navigation"
        through, on the theory that a click cannot compose an order. It can: a
        click opens a ticket, digit keys fill its quantity, Enter submits it, and
        a click on the Orders tab cancels one. A model denied `type_text` falls
        back to exactly that, one key at a time. On a real account the only
        input set that provably cannot reach an order is the empty one.

        The executor also declines to open an order block, but that is the
        polite half; this is the half a future code path cannot forget.
        """
        if self.mode.permits_order_tickets:
            return
        if risk is ActionRisk.READ_ONLY:
            return
        self.stats.calls_blocked += 1
        self.stats.last_block_reason = f"{self.mode.value} forbids all input"
        self._bus.publish(
            EventKind.GUARD,
            f"BLOCKED ({self.mode.banner}) before {label}",
            phase="executing",
            data={"mode": self.mode.value, "risk": risk.value},
        )
        raise OrderTicketsForbidden(
            f"{self.mode.banner}: refusing {label}. This mode only reads the "
            f"screen; it never clicks, types or presses a key on a real account."
        )

    def _assert_permits(self, risk: ActionRisk, label: str) -> None:
        verdict = self._verdict
        if verdict is None:
            self.stats.calls_blocked += 1
            raise DemoModeViolation(f"no demo assertion available before {label}")

        if self.mode.permits_live_account:
            # On a live rung the verdict reports which account is on screen; it
            # does not authorise the call. It cannot: `CONFIRMED_DEMO` is the
            # only state that authorises anything, and an operator with no
            # simulator account can never reach it, so gating here would mean
            # the bot could not read the portfolio it was pointed at.
            #
            # What carries the weight instead differs by rung, and both are
            # enforced above rather than here:
            #
            #   LIVE_READ_ONLY     `_require_mode_allows` has already refused
            #                      every order-critical primitive, so nothing
            #                      reachable from this point can compose an
            #                      order at all.
            #   LIVE_PREPARE_ONLY  a ticket *can* be composed, so the narrower
            #                      gate is `_require_not_a_submit`: no Enter and
            #                      no click inside the submit fence, leaving the
            #                      commit to the operator's own press.
            #
            # Adding a rung that permits orders on a live account would leave
            # this return standing with nothing behind it. Anything new here
            # must bring its own gate before it reaches this line.
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

    async def _post_verify(self, description: str, opened_as: DemoState) -> None:
        """Confirm the screen did not change identity during the order block.

        On the simulator rung this is the original question -- the block can only
        open on `CONFIRMED_DEMO`, so "unchanged" and "still the simulator" are
        the same check.

        On a live rung "still the simulator" has no meaning: the screen was never
        the simulator. The question it was really asking generalises, though. A
        ticket opened on a real account and closing on anything else -- a
        different verdict, a screen the probes can no longer read -- means
        something moved under the bot mid-flow, and that is exactly the case
        this step exists to catch.
        """
        verdict = await self._refresh_verdict(ActionRisk.ORDER_CRITICAL, force=True)
        if verdict.state is opened_as:
            self._bus.publish(
                EventKind.GUARD,
                f"order block CLOSED, screen unchanged ({opened_as.value}): {description}",
                phase="executing",
                data=verdict.to_json(),
            )
            return
        message = (
            f"post-order verification FAILED after {description}: opened on "
            f"{opened_as.value}, closed on {verdict.state.value} ({verdict.detail})"
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

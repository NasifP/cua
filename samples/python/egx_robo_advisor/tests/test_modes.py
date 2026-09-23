"""LIVE_READ_ONLY: a real account may be observed, and never traded on.

The claim these tests defend is narrow and absolute: **no path reachable in
`LIVE_READ_ONLY` can place an order.** Not "the executor declines to", not "the
prompt says not to" -- nothing reachable. So they go at it from several
directions: the primitive, the order block, the executor, and an agent
deliberately trying to type into a ticket.
"""

from pathlib import Path

import pytest

from egx_advisor.bus import StateBus
from egx_advisor.safety.demo_guard import DemoGuard, DemoModeViolation
from egx_advisor.safety.guarded_interface import (
    GuardedInterface,
    KillSwitchEngaged,
    SubmitFence,
)
from egx_advisor.safety.modes import (
    ExecutionMode,
    OrderTicketsForbidden,
    SubmitFenceMissing,
    SubmitForbidden,
)
from egx_advisor.safety.pngutil import encode_png

BLANK = encode_png(8, 8, bytes(8 * 8 * 3))


class FakeInterface:
    def __init__(self, labels=("Real Account",)):
        self.labels = list(labels)
        self.calls: list[tuple] = []

    @property
    def tree(self) -> dict:
        return {"children": [{"label": x} for x in self.labels]}

    async def screenshot(self) -> bytes:
        return BLANK

    async def get_screen_size(self) -> dict:
        return {"width": 1920, "height": 1080}

    async def left_click(self, x: int, y: int) -> None:
        self.calls.append(("left_click", x, y))

    async def scroll_down(self, clicks: int = 1) -> None:
        self.calls.append(("scroll_down", clicks))

    async def type_text(self, text: str) -> None:
        self.calls.append(("type_text", text))

    async def press_key(self, key: str) -> None:
        self.calls.append(("press_key", key))

    async def brand_new_primitive(self, payload: str) -> None:
        self.calls.append(("brand_new_primitive", payload))


def build(tmp_path: Path, *, mode: ExecutionMode, labels=("Real Account",)):
    bus = StateBus(tmp_path / "state.db")
    bus.resume(actor="test", reason="armed")
    raw = FakeInterface(labels)
    guarded = GuardedInterface(
        raw,
        bus=bus,
        guard=DemoGuard(text_probes=(), colour_probe=None),
        accessibility_tree_provider=lambda: raw.tree,
        mode=mode,
    )
    return bus, raw, guarded


# ----------------------------------------------------------------- the ladder


def test_no_mode_submits_an_order_on_a_real_account() -> None:
    """The invariant, restated.

    It used to be "no live rung may open a ticket", which was the same thing
    while no rung could fill one. `LIVE_PREPARE_ONLY` fills a real ticket and
    stops, so the line moved from opening to committing -- and every live rung
    that can fill one must carry the fence that stops it committing, or the
    distinction is a name rather than a check.
    """
    assert {m.value for m in ExecutionMode} == {
        "simulator_only",
        "live_read_only",
        "live_prepare_only",
    }
    for mode in ExecutionMode:
        if not mode.permits_live_account:
            continue
        assert not mode.permits_orders, f"{mode} must never submit on a real account"
        if mode.permits_order_tickets:
            assert mode.requires_submit_fence, (
                f"{mode} can fill a real ticket, so it must be unable to start "
                f"without the no-click fence around submit"
            )


def test_parse_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown execution mode"):
        ExecutionMode.parse("live_armed")


# --------------------------------------------------- observing a real account


async def test_a_real_account_no_longer_halts_the_bot(tmp_path: Path) -> None:
    bus, raw, guarded = build(tmp_path, mode=ExecutionMode.LIVE_READ_ONLY)
    verdict = await guarded.assert_demo_now()
    assert verdict.state.value == "confirmed_live"
    assert not bus.is_halted(), "a real account is expected on this rung"


async def test_reading_and_navigating_a_real_account_is_permitted(tmp_path: Path) -> None:
    """Otherwise the bot cannot reach the holdings it was pointed at."""
    _, raw, guarded = build(tmp_path, mode=ExecutionMode.LIVE_READ_ONLY)
    assert await guarded.get_screen_size() == {"width": 1920, "height": 1080}
    await guarded.left_click(100, 200)
    await guarded.scroll_down(3)
    assert ("left_click", 100, 200) in raw.calls


async def test_simulator_mode_still_halts_on_a_real_account(tmp_path: Path) -> None:
    """The default rung is unchanged by any of this."""
    bus, raw, guarded = build(tmp_path, mode=ExecutionMode.SIMULATOR_ONLY)
    with pytest.raises(DemoModeViolation):
        await guarded.left_click(1, 1)
    assert raw.calls == []
    assert bus.is_halted()


# ------------------------------------------------- nothing can reach an order


async def test_order_critical_primitives_are_refused(tmp_path: Path) -> None:
    _, raw, guarded = build(tmp_path, mode=ExecutionMode.LIVE_READ_ONLY)
    with pytest.raises(OrderTicketsForbidden, match="never trades"):
        await guarded.type_text("100")
    assert raw.calls == []


async def test_an_unrecognised_primitive_is_refused_too(tmp_path: Path) -> None:
    """Unknown methods are order-critical by default, so they are refused here."""
    _, raw, guarded = build(tmp_path, mode=ExecutionMode.LIVE_READ_ONLY)
    with pytest.raises(OrderTicketsForbidden):
        await guarded.brand_new_primitive("anything")
    assert raw.calls == []


async def test_the_order_block_refuses_to_open(tmp_path: Path) -> None:
    _, raw, guarded = build(tmp_path, mode=ExecutionMode.LIVE_READ_ONLY)
    with pytest.raises(OrderTicketsForbidden, match="order block"):
        async with guarded.order_critical("BUY COMI.CA"):
            pytest.fail("the block body must never run")
    assert raw.calls == []


async def test_navigation_inside_a_would_be_order_block_is_refused(tmp_path: Path) -> None:
    """Elevation cannot be obtained, so clicks cannot be laundered through it."""
    _, raw, guarded = build(tmp_path, mode=ExecutionMode.LIVE_READ_ONLY)
    with pytest.raises(OrderTicketsForbidden):
        async with guarded.order_critical("BUY COMI.CA"):
            await guarded.left_click(412, 880)
    assert not guarded.elevated
    assert raw.calls == []


async def test_the_executor_declines_to_submit(tmp_path: Path) -> None:
    from decimal import Decimal as D

    from egx_advisor.execution.thndr import ExecutionError, ThndrExecutor, ThndrUiMap
    from egx_advisor.types import ProposedOrder, Side

    bus, raw, guarded = build(tmp_path, mode=ExecutionMode.LIVE_READ_ONLY)
    executor = ThndrExecutor(
        interface=guarded,
        bus=bus,
        # Even fully calibrated, the mode refuses first.
        ui=ThndrUiMap(calibration_complete=True),
        agent=object(),
    )
    order = ProposedOrder("COMI.CA", Side.BUY, D(100), D(85), "drift")

    with pytest.raises(ExecutionError, match="never trades"):
        await executor.submit_order(order)
    assert raw.calls == []


async def test_the_kill_switch_still_stops_observation(tmp_path: Path) -> None:
    bus, raw, guarded = build(tmp_path, mode=ExecutionMode.LIVE_READ_ONLY)
    await guarded.left_click(1, 1)
    bus.halt(actor="mobile", reason="KILL SWITCH")
    with pytest.raises(KillSwitchEngaged):
        await guarded.left_click(2, 2)
    assert len(raw.calls) == 1


# ------------------------------------------------------------ agent wiring


def test_config_forces_execution_off_on_an_observe_only_mode() -> None:
    """Two settings that disagree is a fourth state nobody reasoned about."""
    from egx_advisor.egx_cua_agent import AgentConfig

    config = AgentConfig(execute_orders=True, mode=ExecutionMode.LIVE_READ_ONLY)
    assert config.execute_orders is False

    unchanged = AgentConfig(execute_orders=True, mode=ExecutionMode.SIMULATOR_ONLY)
    assert unchanged.execute_orders is True

    # The prepare rung has real work to do -- filling tickets for the operator
    # to submit -- so switching execution off there would make it inert.
    prepares = AgentConfig(execute_orders=True, mode=ExecutionMode.LIVE_PREPARE_ONLY)
    assert prepares.execute_orders is True


async def test_ensure_simulator_observes_rather_than_switching(tmp_path: Path) -> None:
    """On a real account there is nothing to switch to, and nothing fatal."""
    from egx_advisor.execution.thndr import ThndrExecutor, ThndrUiMap

    bus, raw, guarded = build(tmp_path, mode=ExecutionMode.LIVE_READ_ONLY)
    executor = ThndrExecutor(interface=guarded, bus=bus, ui=ThndrUiMap(), agent=None)

    await executor.ensure_simulator()  # must not raise, must not click

    assert raw.calls == []
    messages = [e.message for e in bus.recent_events(limit=20)]
    assert any("REAL ACCOUNT" in m and "confirmed_live" in m for m in messages)


# ------------------------------------------- LIVE_PREPARE_ONLY: fill, never submit

FENCE = SubmitFence(left=300, top=840, right=560, bottom=920, label="Buy button")


def build_prepare(tmp_path: Path, *, fence=FENCE):
    bus = StateBus(tmp_path / "state.db")
    bus.resume(actor="test", reason="armed")
    raw = FakeInterface(("Real Account",))
    guarded = GuardedInterface(
        raw,
        bus=bus,
        guard=DemoGuard(text_probes=(), colour_probe=None),
        accessibility_tree_provider=lambda: raw.tree,
        mode=ExecutionMode.LIVE_PREPARE_ONLY,
        submit_fence=fence,
    )
    return bus, raw, guarded


def test_the_prepare_rung_refuses_to_start_without_a_fence(tmp_path: Path) -> None:
    """Discovering this in front of a filled ticket would be the worst moment."""
    with pytest.raises(SubmitFenceMissing, match="submit_fence"):
        build_prepare(tmp_path, fence=None)


def test_an_empty_fence_is_rejected_rather_than_accepted(tmp_path: Path) -> None:
    """A zero-area rectangle blocks nothing while looking like a protection."""
    with pytest.raises(ValueError, match="empty or inverted"):
        SubmitFence(left=300, top=840, right=300, bottom=920)


async def test_the_ticket_can_be_filled(tmp_path: Path) -> None:
    """The whole point of the rung: it must actually be able to prepare an order."""
    _, raw, guarded = build_prepare(tmp_path)
    async with guarded.order_critical("BUY COMI.CA x100"):
        await guarded.left_click(400, 300)      # the quantity field, above the fence
        await guarded.type_text("100")
    assert ("left_click", 400, 300) in raw.calls
    assert ("type_text", "100") in raw.calls


async def test_enter_inside_an_order_block_is_refused(tmp_path: Path) -> None:
    """The route that submits without anything resembling a click on Buy."""
    _, raw, guarded = build_prepare(tmp_path)
    with pytest.raises(SubmitForbidden, match="Enter commits the ticket"):
        async with guarded.order_critical("BUY COMI.CA x100"):
            await guarded.press_key("enter")
    assert ("press_key", "enter") not in raw.calls


async def test_a_newline_in_typed_text_is_refused(tmp_path: Path) -> None:
    """`type_text("100\\n")` submits a form as surely as pressing the key."""
    _, raw, guarded = build_prepare(tmp_path)
    with pytest.raises(SubmitForbidden):
        async with guarded.order_critical("BUY COMI.CA x100"):
            await guarded.type_text("100\n")
    assert raw.calls == []


async def test_enter_outside_an_order_block_is_still_allowed(tmp_path: Path) -> None:
    """Enter is a submit only once a ticket is open.

    A guard that refused it everywhere -- dismissing a dialog, searching a
    watchlist -- would cry wolf, and a guard that cries wolf gets switched off.
    """
    _, raw, guarded = build_prepare(tmp_path)
    await guarded.press_key("enter")
    assert ("press_key", "enter") in raw.calls


async def test_a_click_inside_the_submit_fence_is_refused(tmp_path: Path) -> None:
    """"The bot does not press Buy" as a geometric check, not a promise."""
    _, raw, guarded = build_prepare(tmp_path)
    with pytest.raises(SubmitForbidden, match="Buy button"):
        async with guarded.order_critical("BUY COMI.CA x100"):
            await guarded.left_click(430, 880)   # dead centre of the fence
    assert raw.calls == []


async def test_the_fence_holds_at_its_edges(tmp_path: Path) -> None:
    """Off-by-one here is the difference between blocked and an order placed."""
    _, raw, guarded = build_prepare(tmp_path)
    async with guarded.order_critical("BUY COMI.CA x100"):
        await guarded.left_click(299, 880)       # one pixel left of the fence
        await guarded.left_click(430, 839)       # one pixel above it
    assert len(raw.calls) == 2

    for inside in ((300, 840), (560, 920), (300, 920), (560, 840)):
        _, raw2, guarded2 = build_prepare(tmp_path / str(inside))
        with pytest.raises(SubmitForbidden):
            async with guarded2.order_critical("BUY"):
                await guarded2.left_click(*inside)
        assert raw2.calls == [], f"{inside} is on the fence and must be refused"


async def test_an_unreadable_click_target_is_refused_not_waved_through(tmp_path: Path) -> None:
    """A pointer call we cannot fence is a pointer call we cannot allow.

    Otherwise the fence quietly becomes optional for anything whose signature
    the SDK changes.
    """
    _, raw, guarded = build_prepare(tmp_path)
    with pytest.raises(SubmitForbidden, match="cannot read the click target"):
        async with guarded.order_critical("BUY COMI.CA x100"):
            await guarded.left_click(x="middle", y="bottom")
    assert raw.calls == []


async def test_the_simulator_rung_is_not_fenced(tmp_path: Path) -> None:
    """It submits on purpose, and has nothing at stake when it does."""
    bus = StateBus(tmp_path / "state.db")
    bus.resume(actor="test", reason="armed")
    raw = FakeInterface(("Simulator",))
    guarded = GuardedInterface(
        raw,
        bus=bus,
        guard=DemoGuard(text_probes=(), colour_probe=None),
        accessibility_tree_provider=lambda: raw.tree,
        mode=ExecutionMode.SIMULATOR_ONLY,
    )
    async with guarded.order_critical("BUY COMI.CA x100"):
        await guarded.press_key("enter")
    assert ("press_key", "enter") in raw.calls


async def test_a_ticket_that_changes_screens_mid_flow_still_fails(tmp_path: Path) -> None:
    """Post-verification generalises rather than being dropped on this rung.

    "Still the simulator" is meaningless here -- it never was one. "Still the
    screen you opened the ticket on" is the question that was actually being
    asked, and it survives the move to a live account.
    """
    bus, raw, guarded = build_prepare(tmp_path)
    with pytest.raises(DemoModeViolation, match="opened on confirmed_live"):
        async with guarded.order_critical("BUY COMI.CA x100"):
            await guarded.type_text("100")
            raw.labels = ["Simulator"]      # the screen moved under the bot
    assert bus.is_halted(), "an unexplained screen change latches the bot off"


# --------------------------------------- the executor on the prepare rung


class ScriptedAgent:
    """A ComputerAgent that drives the guarded interface, as the real one does."""

    def __init__(self, guarded, script):
        self.guarded = guarded
        self.script = script
        self.instructions: list[str] = []

    async def run(self, history, stream=False):
        self.instructions.append(history[0]["content"])
        for call in self.script:
            method, *rest = call
            await getattr(self.guarded, method)(*rest)
        yield {"output": [{"type": "message", "content": [{"text": "done"}]}]}


def an_order():
    from decimal import Decimal as D

    from egx_advisor.types import ProposedOrder, Side

    return ProposedOrder("COMI.CA", Side.BUY, D(100), D(85), "drift")


def executor_for(guarded, bus, agent=None):
    from egx_advisor.execution.thndr import ThndrExecutor, ThndrUiMap

    return ThndrExecutor(
        interface=guarded,
        bus=bus,
        ui=ThndrUiMap(calibration_complete=True),
        agent=agent,
    )


async def test_submit_is_refused_on_the_prepare_rung(tmp_path: Path) -> None:
    """Filling a ticket is permitted here. Committing one is not, ever."""
    from egx_advisor.execution.thndr import ExecutionError

    bus, raw, guarded = build_prepare(tmp_path)
    executor = executor_for(guarded, bus, agent=object())

    with pytest.raises(ExecutionError, match="never trades"):
        await executor.submit_order(an_order())
    assert raw.calls == []


async def test_prepare_is_refused_on_the_read_only_rung(tmp_path: Path) -> None:
    """The rungs do not leak into each other: read-only opens no ticket at all."""
    from egx_advisor.execution.thndr import ExecutionError

    bus, raw, guarded = build(tmp_path, mode=ExecutionMode.LIVE_READ_ONLY)
    executor = executor_for(guarded, bus, agent=object())

    with pytest.raises(ExecutionError, match="never opens a ticket"):
        await executor.prepare_order(an_order())
    assert raw.calls == []


async def test_a_prepared_ticket_is_not_journalled_as_an_order(tmp_path: Path) -> None:
    """A prepared ticket is not a fill, and the journal must not imply one."""
    bus, raw, guarded = build_prepare(tmp_path)
    agent = ScriptedAgent(guarded, [("left_click", 400, 300), ("type_text", "100")])
    await executor_for(guarded, bus, agent=agent).prepare_order(an_order())

    messages = [e.message for e in bus.recent_events(limit=50)]
    assert any("TICKET READY, NOT SUBMITTED" in m for m in messages)
    assert not any(m.startswith("submitted ") for m in messages)
    assert ("type_text", "100") in raw.calls, "it must actually fill the ticket"


async def test_an_agent_that_presses_enter_is_stopped_by_the_guard(tmp_path: Path) -> None:
    """The instruction says do not submit. The guard is what makes that true.

    A model that ignores the prompt, or a future flow that forgets it, still
    cannot commit the ticket: the refusal is at the proxy.
    """
    bus, raw, guarded = build_prepare(tmp_path)
    disobedient = ScriptedAgent(
        guarded, [("type_text", "100"), ("press_key", "enter")]
    )

    # Specifically the proxy's refusal -- not some unrelated failure that
    # happens to stop the flow and would make this test pass for the wrong
    # reason.
    with pytest.raises(SubmitForbidden, match="Enter commits the ticket"):
        await executor_for(guarded, bus, agent=disobedient).prepare_order(an_order())

    assert ("press_key", "enter") not in raw.calls
    messages = [e.message for e in bus.recent_events(limit=50)]
    assert not any("TICKET READY" in m for m in messages), (
        "a blocked submit must not still report a ready ticket"
    )

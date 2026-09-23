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
from egx_advisor.safety.guarded_interface import GuardedInterface, KillSwitchEngaged
from egx_advisor.safety.modes import ExecutionMode, OrderTicketsForbidden
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


def test_only_two_modes_exist_and_neither_trades_on_a_live_account() -> None:
    """An enum member nothing enforces is worse than one that does not exist."""
    assert {m.value for m in ExecutionMode} == {"simulator_only", "live_read_only"}
    for mode in ExecutionMode:
        if mode.permits_live_account:
            assert not mode.permits_orders
            assert not mode.permits_order_tickets


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


async def test_ensure_simulator_observes_rather_than_switching(tmp_path: Path) -> None:
    """On a real account there is nothing to switch to, and nothing fatal."""
    from egx_advisor.execution.thndr import ThndrExecutor, ThndrUiMap

    bus, raw, guarded = build(tmp_path, mode=ExecutionMode.LIVE_READ_ONLY)
    executor = ThndrExecutor(interface=guarded, bus=bus, ui=ThndrUiMap(), agent=None)

    await executor.ensure_simulator()  # must not raise, must not click

    assert raw.calls == []
    messages = [e.message for e in bus.recent_events(limit=20)]
    assert any("REAL ACCOUNT" in m and "confirmed_live" in m for m in messages)

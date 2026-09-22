"""The chokepoint: no input reaches the screen without passing both gates."""

from pathlib import Path

import pytest

from egx_advisor.bus import StateBus
from egx_advisor.safety.demo_guard import ActionRisk, DemoGuard, DemoModeViolation
from egx_advisor.safety.guarded_interface import (
    GuardedInterface,
    KillSwitchEngaged,
    classify,
)
from egx_advisor.safety.pngutil import encode_png

BLANK = encode_png(8, 8, bytes(8 * 8 * 3))


class FakeInterface:
    """Records what actually reached the screen."""

    def __init__(self, labels=("Simulator",)):
        self.labels = list(labels)
        self.calls: list[tuple] = []

    @property
    def tree(self) -> dict:
        return {"children": [{"label": label} for label in self.labels]}

    async def screenshot(self) -> bytes:
        return BLANK

    async def left_click(self, x: int, y: int) -> None:
        self.calls.append(("left_click", x, y))

    async def type_text(self, text: str) -> None:
        self.calls.append(("type_text", text))

    async def scroll_down(self, clicks: int = 1) -> None:
        self.calls.append(("scroll_down", clicks))

    async def get_screen_size(self) -> dict:
        self.calls.append(("get_screen_size",))
        return {"width": 1170, "height": 2532}

    async def brand_new_primitive(self, payload: str) -> None:
        """Stands in for a method a future SDK version might add."""
        self.calls.append(("brand_new_primitive", payload))


@pytest.fixture
def armed(tmp_path: Path):
    bus = StateBus(tmp_path / "state.db")
    bus.resume(actor="test", reason="armed")
    raw = FakeInterface()
    guarded = GuardedInterface(
        raw,
        bus=bus,
        guard=DemoGuard(text_probes=(), colour_probe=None),
        accessibility_tree_provider=lambda: raw.tree,
    )
    return bus, raw, guarded


# ------------------------------------------------------------- classification


def test_known_input_methods_are_classified() -> None:
    assert classify("screenshot") is ActionRisk.READ_ONLY
    assert classify("left_click") is ActionRisk.NAVIGATION
    assert classify("type_text") is ActionRisk.ORDER_CRITICAL


def test_unknown_methods_default_to_the_strictest_tier() -> None:
    """A new SDK primitive must not slip past the guard as 'probably harmless'."""
    assert classify("some_future_tap_method") is ActionRisk.ORDER_CRITICAL


# ---------------------------------------------------------------- kill switch


async def test_kill_switch_blocks_input(armed) -> None:
    bus, raw, guarded = armed
    await guarded.left_click(1, 2)
    assert len(raw.calls) == 1

    bus.halt(actor="mobile", reason="KILL SWITCH")
    with pytest.raises(KillSwitchEngaged):
        await guarded.left_click(3, 4)
    assert len(raw.calls) == 1, "no further call may reach the screen"


async def test_kill_switch_blocks_read_only_calls_too(armed) -> None:
    """Halting means halting. A stopped bot does not keep screenshotting either."""
    bus, raw, guarded = armed
    bus.halt(actor="mobile", reason="KILL SWITCH")
    with pytest.raises(KillSwitchEngaged):
        await guarded.get_screen_size()


async def test_kill_switch_set_by_another_handle_is_honoured(armed, tmp_path) -> None:
    """The dashboard is a separate process writing to the same bus file."""
    bus, raw, guarded = armed
    dashboard = StateBus(tmp_path / "state.db")
    dashboard.halt(actor="mobile:pola", reason="from the phone")
    with pytest.raises(KillSwitchEngaged, match="mobile:pola"):
        await guarded.left_click(1, 1)


# ------------------------------------------------------------ demo assertion


async def test_navigation_allowed_on_a_confirmed_simulator(armed) -> None:
    _, raw, guarded = armed
    await guarded.left_click(10, 20)
    assert ("left_click", 10, 20) in raw.calls


async def test_input_blocked_when_demo_is_unconfirmed(armed) -> None:
    _, raw, guarded = armed
    raw.labels = ["Portfolio"]  # no simulator marker anywhere
    with pytest.raises(DemoModeViolation):
        await guarded.left_click(1, 1)
    assert raw.calls == []


async def test_read_only_still_works_when_demo_is_unconfirmed(armed) -> None:
    """We must be able to look at the screen in order to assert anything about it."""
    _, raw, guarded = armed
    raw.labels = ["Portfolio"]
    assert await guarded.get_screen_size() == {"width": 1170, "height": 2532}


async def test_account_flipping_to_live_is_caught_on_the_very_next_call(armed) -> None:
    """The freshness rule exists for exactly this: a switch mid-session."""
    bus, raw, guarded = armed
    await guarded.left_click(1, 1)
    raw.labels = ["Real Account"]
    before = len(raw.calls)

    with pytest.raises(DemoModeViolation):
        await guarded.left_click(2, 2)

    assert len(raw.calls) == before, "not one click may land on a live account"
    assert bus.is_halted(), "seeing real money must latch the kill switch"


async def test_live_detection_halts_the_bus_permanently(armed) -> None:
    bus, raw, guarded = armed
    raw.labels = ["Real Money"]
    with pytest.raises(DemoModeViolation):
        await guarded.left_click(1, 1)
    assert bus.is_halted()
    assert "real-money" in bus.control_state().reason


async def test_screenshot_failure_blocks_instead_of_assuming(armed) -> None:
    """If we cannot see the screen, we cannot claim it is the simulator."""
    _, raw, guarded = armed

    async def broken_screenshot() -> bytes:
        raise RuntimeError("display gone")

    raw.screenshot = broken_screenshot  # type: ignore[method-assign]
    with pytest.raises(DemoModeViolation):
        await guarded.left_click(1, 1)
    assert raw.calls == []


# --------------------------------------------------------------- order blocks


async def test_order_block_elevates_navigation_to_order_critical(armed) -> None:
    _, raw, guarded = armed
    async with guarded.order_critical("BUY COMI.CA"):
        assert guarded.elevated
        await guarded.left_click(412, 880)
    assert not guarded.elevated
    assert ("left_click", 412, 880) in raw.calls


async def test_order_block_post_verification_catches_a_mid_flow_switch(armed) -> None:
    """The check the pre-assertion structurally cannot make."""
    bus, raw, guarded = armed
    with pytest.raises(DemoModeViolation, match="post-order verification"):
        async with guarded.order_critical("BUY COMI.CA"):
            await guarded.left_click(412, 880)
            raw.labels = ["Real Account"]  # account switches during the flow
    assert bus.is_halted()


async def test_order_block_refuses_to_open_on_an_unconfirmed_screen(armed) -> None:
    _, raw, guarded = armed
    raw.labels = ["Portfolio"]
    with pytest.raises(DemoModeViolation):
        async with guarded.order_critical("BUY COMI.CA"):
            pytest.fail("the block body must never run")


# -------------------------------------------------------------------- logging


async def test_actions_are_journalled_before_they_happen(armed) -> None:
    """A log written after the fact is missing the entry you need after a crash."""
    bus, raw, guarded = armed
    await guarded.left_click(412, 880)
    ui_events = [e for e in bus.recent_events(limit=50) if e.kind == "ui_action"]
    assert any("left_click(412, 880)" in e.message for e in ui_events)


async def test_journal_clamps_long_arguments(armed) -> None:
    """The journal must stay readable: no screenshot blobs, no unbounded strings."""
    bus, raw, guarded = armed
    await guarded.type_text("x" * 500)

    event = next(
        e for e in bus.recent_events(limit=50)
        if e.kind == "ui_action" and e.data.get("method") == "type_text"
    )
    logged = event.data["args"]["positional"][0]
    assert len(logged) < 200 and logged.endswith("...")


async def test_unknown_primitive_is_gated_as_order_critical(armed) -> None:
    """The new-SDK-method case, end to end."""
    _, raw, guarded = armed
    await guarded.brand_new_primitive("hello")
    assert ("brand_new_primitive", "hello") in raw.calls

    raw.labels = ["Portfolio"]
    with pytest.raises(DemoModeViolation):
        await guarded.brand_new_primitive("again")

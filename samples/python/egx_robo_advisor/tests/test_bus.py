"""The bus, and the fail-safe properties of the kill switch."""

import os
from pathlib import Path

import pytest

from egx_advisor.bus import EventKind, StateBus


@pytest.fixture
def bus_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def test_starts_halted(bus_path: Path) -> None:
    """A fresh bus is halted. Arming is always an explicit act."""
    assert StateBus(bus_path).is_halted()


def test_halt_and_resume_round_trip(bus_path: Path) -> None:
    bus = StateBus(bus_path)
    bus.resume(actor="operator", reason="armed")
    assert not bus.is_halted()
    bus.halt(actor="mobile", reason="kill switch")
    assert bus.is_halted()
    assert bus.control_state().actor == "mobile"


def test_sentinel_file_survives_a_destroyed_database(bus_path: Path) -> None:
    """A corrupt or deleted database must not un-halt the bot."""
    bus = StateBus(bus_path)
    bus.resume(actor="operator", reason="armed")
    bus.halt(actor="mobile", reason="kill switch from phone")
    bus.close()

    os.remove(bus_path)
    revived = StateBus(bus_path)
    assert revived.is_halted()
    assert "kill switch from phone" in revived.control_state().reason


def test_halt_survives_a_wiped_sentinel_via_the_database(bus_path: Path) -> None:
    """And the converse: losing the sentinel still leaves the DB row halted."""
    bus = StateBus(bus_path)
    bus.resume(actor="operator", reason="armed")
    bus.halt(actor="mobile", reason="kill switch")
    bus.sentinel.unlink()
    assert bus.is_halted()


def test_is_halted_fails_closed_when_the_read_raises(bus_path: Path, monkeypatch) -> None:
    bus = StateBus(bus_path)
    bus.resume(actor="operator", reason="armed")
    assert not bus.is_halted()

    def boom() -> None:
        raise RuntimeError("database on fire")

    monkeypatch.setattr(bus, "control_state", boom)
    assert bus.is_halted(), "an unreadable control state must read as halted"


def test_events_are_visible_to_a_second_process_handle(bus_path: Path) -> None:
    agent = StateBus(bus_path)
    dashboard = StateBus(bus_path)

    agent.publish(EventKind.UI_ACTION, "left_click(412, 880)", phase="executing")
    agent.put("regime", {"risk_state": "risk_on"})

    events = dashboard.recent_events(limit=10)
    assert any(e.message == "left_click(412, 880)" for e in events)
    snapshot = dashboard.get("regime")
    assert snapshot is not None and snapshot["payload"]["risk_state"] == "risk_on"


def test_control_written_by_dashboard_is_seen_by_agent(bus_path: Path) -> None:
    agent = StateBus(bus_path)
    dashboard = StateBus(bus_path)
    agent.resume(actor="operator", reason="armed")

    dashboard.halt(actor="mobile:pola", reason="KILL SWITCH")

    assert agent.is_halted()
    assert agent.control_state().actor == "mobile:pola"


def test_events_since_is_a_cursor(bus_path: Path) -> None:
    bus = StateBus(bus_path)
    first = bus.publish(EventKind.LIFECYCLE, "one")
    second = bus.publish(EventKind.LIFECYCLE, "two")
    assert [e.seq for e in bus.events_since(first)] == [second]
    assert bus.events_since(second) == []


def test_snapshot_serialises_decimals_and_enums(bus_path: Path) -> None:
    from decimal import Decimal

    bus = StateBus(bus_path)
    bus.put("plan", {"turnover": Decimal("1234.56"), "kind": EventKind.PLAN})
    payload = bus.get("plan")
    assert payload is not None
    assert payload["payload"] == {"turnover": "1234.56", "kind": "plan"}


def test_control_audit_records_every_transition(bus_path: Path) -> None:
    bus = StateBus(bus_path)
    bus.resume(actor="a", reason="armed")
    bus.halt(actor="b", reason="stop")
    actors = [row["actor"] for row in bus.control_audit()]
    assert "a" in actors and "b" in actors

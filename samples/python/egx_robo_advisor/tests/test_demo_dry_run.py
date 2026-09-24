"""The dry-run harness must leave nothing open when it finishes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import demo_dry_run
from egx_advisor.bus import StateBus


def _is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


async def test_safety_scenario_closes_every_bus_it_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows refuses to delete a database file SQLite still holds open.

    The harness runs inside a TemporaryDirectory, so one connection left open
    turned a successful run into a PermissionError at exit on Windows. Linux
    deletes open files without complaint, which is why this has to be checked
    directly rather than by watching the cleanup.
    """
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    await demo_dry_run.scenario_safety(tmp_path)

    assert opened, "the scenario should have opened at least one bus"
    still_open = [c for c in opened if not _is_closed(c)]
    assert not still_open, f"{len(still_open)} of {len(opened)} connections left open"


def test_bus_closes_on_leaving_a_with_block(tmp_path: Path) -> None:
    with StateBus(tmp_path / "bus.db") as bus:
        bus.halt(actor="test", reason="context manager")
        conn = bus._local.conn
    assert _is_closed(conn)

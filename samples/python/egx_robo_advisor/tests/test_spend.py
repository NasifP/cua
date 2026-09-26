"""The daily model budget: one ledger for every process, and it actually stops calls."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from egx_advisor.bus import StateBus
from egx_advisor.spend import BudgetExceeded, describe, metered, today


def fake_completion(**kwargs):
    return {"choices": [{"message": {"content": "ok"}}]}


def make(tmp_path: Path, env: dict, cost=lambda r, m: 0.01, now=None):
    bus = StateBus(tmp_path / "bus.db")
    call = metered(
        fake_completion, purpose="test", bus_factory=lambda: bus, env=env,
        cost=cost, now=(lambda: now),
    )
    return call, bus


def test_calls_are_counted_and_priced(tmp_path) -> None:
    call, bus = make(tmp_path, {})
    call(model="gemini/x", messages=[])
    call(model="gemini/x", messages=[])
    totals = today(bus)
    assert totals["calls"] == 2
    assert totals["usd"] == pytest.approx(0.02)
    assert totals["by_purpose"]["test"]["calls"] == 2


def test_the_call_limit_stops_the_next_call(tmp_path) -> None:
    call, _ = make(tmp_path, {"EGX_DAILY_CALL_LIMIT": "2"})
    call(model="m", messages=[])
    call(model="m", messages=[])
    with pytest.raises(BudgetExceeded, match="call limit"):
        call(model="m", messages=[])


def test_the_dollar_limit_stops_the_next_call(tmp_path) -> None:
    call, _ = make(tmp_path, {"EGX_DAILY_SPEND_LIMIT_USD": "0.05"}, cost=lambda r, m: 0.03)
    call(model="m", messages=[])
    call(model="m", messages=[])
    with pytest.raises(BudgetExceeded, match="spend limit"):
        call(model="m", messages=[])


def test_an_unpriced_model_still_counts_against_the_call_limit(tmp_path) -> None:
    """The cua trajectory budget never tripped because the cost was never set."""
    call, bus = make(tmp_path, {"EGX_DAILY_CALL_LIMIT": "1"}, cost=lambda r, m: None)
    call(model="new/model", messages=[])
    assert today(bus)["unpriced_calls"] == 1
    assert "1 unpriced" in describe(today(bus), {})
    with pytest.raises(BudgetExceeded):
        call(model="new/model", messages=[])


def test_two_processes_share_one_budget(tmp_path) -> None:
    """The agent and the dashboard each open the bus; the ledger is on it."""
    first, _ = make(tmp_path, {"EGX_DAILY_CALL_LIMIT": "2"})
    second = metered(
        fake_completion, purpose="chat",
        bus_factory=lambda: StateBus(tmp_path / "bus.db"),
        env={"EGX_DAILY_CALL_LIMIT": "2"}, cost=lambda r, m: 0.0,
    )
    first(model="m", messages=[])
    second(model="m", messages=[])
    with pytest.raises(BudgetExceeded):
        first(model="m", messages=[])


def test_a_new_day_starts_from_zero(tmp_path) -> None:
    yesterday = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    old, bus = make(tmp_path, {"EGX_DAILY_CALL_LIMIT": "1"}, now=yesterday)
    old(model="m", messages=[])
    new = metered(
        fake_completion, purpose="test", bus_factory=lambda: bus,
        env={"EGX_DAILY_CALL_LIMIT": "1"}, cost=lambda r, m: 0.0,
        now=lambda: datetime(2026, 9, 25, 12, tzinfo=timezone.utc),
    )
    new(model="m", messages=[])  # must not raise


def test_a_failed_call_is_not_counted(tmp_path) -> None:
    bus = StateBus(tmp_path / "bus.db")

    def broken(**kwargs):
        raise TimeoutError("provider down")

    call = metered(broken, purpose="t", bus_factory=lambda: bus, env={})
    with pytest.raises(TimeoutError):
        call(model="m", messages=[])
    assert today(bus)["calls"] == 0


def test_every_model_call_site_is_metered() -> None:
    """A new call site that skips the ledger reopens the hole the cua budget had."""
    root = Path(__file__).resolve().parent.parent / "egx_advisor"
    for rel in ("assistant.py", "regime/sentiment.py", "execution/thndr.py"):
        source = (root / rel).read_text(encoding="utf-8")
        assert "metered(" in source, rel


def test_the_budget_is_in_pounds_at_the_days_rate(tmp_path):
    from egx_advisor.bus import StateBus
    from egx_advisor.spend import in_egp, limits, usd_egp

    bus = StateBus(tmp_path / "fx.db")
    assert usd_egp(bus) == (50.0, False)  # no rate recorded yet
    bus.put("fx", {"usd_egp": "48.50"})
    rate, measured = usd_egp(bus)
    assert (rate, measured) == (48.5, True)
    assert limits({"EGX_DAILY_SPEND_LIMIT_EGP": "97"}, rate)[1] == pytest.approx(2.0)
    assert in_egp({"usd": 1.0}, {"EGX_DAILY_SPEND_LIMIT_EGP": "97"}, rate) == pytest.approx(
        (48.5, 97.0))
    # The old dollar setting still works when no pound limit is set.
    assert limits({"EGX_DAILY_SPEND_LIMIT_USD": "3"}, rate)[1] == 3.0
    assert "EGP" in describe({"usd": 0.1, "calls": 1}, {}, rate)

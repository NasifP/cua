"""The Learn tab's checkpoints: ticked by evidence only, and lost with it."""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from decimal import Decimal

import pytest

from egx_advisor import curriculum as cur
from egx_advisor.memory import Memory, Pick


class Row:
    def __init__(self, day: date, close: float) -> None:
        self.day, self.close = day, Decimal(str(close))


class Archive:
    """series(symbol, since) over a fixed price path per symbol."""

    def __init__(self, paths: dict[str, list[Row]]) -> None:
        self.paths = paths

    def series(self, symbol, since=None):
        return [r for r in self.paths.get(symbol, []) if since is None or r.day >= since]


def _rows(start: date, closes: list[float]) -> list[Row]:
    return [Row(start + timedelta(days=i), c) for i, c in enumerate(closes)]


def by_key(statuses):
    return {s.checkpoint.key: s for s in statuses}


def test_every_checkpoint_has_both_languages_and_a_known_stage():
    keys = [cp.key for cp in cur.CHECKPOINTS]
    assert len(keys) == len(set(keys))
    for cp in cur.CHECKPOINTS:
        assert 1 <= cp.stage <= len(cur.STAGES)
        assert all(cp.title) and all(cp.goal)


def test_nothing_is_ticked_without_evidence_and_speculation_stays_locked(tmp_path):
    memory = Memory(tmp_path / "m.db")
    statuses = cur.evaluate(cur.gather(memory, config_dir=tmp_path, universe=["A.CA"]))
    assert all(s.state != "done" for s in statuses)
    locked = {s.checkpoint.key for s in statuses if s.state == "locked"}
    assert {"intraday", "paper", "loss_limits", "paper_3m"} <= locked
    assert all(s.checkpoint.stage >= 5 for s in statuses if s.state == "locked")


def test_evidence_ticks_the_matching_checkpoints(tmp_path):
    memory = Memory(tmp_path / "m.db")
    memory.add_note("preference", "I prefer banks")
    for i in range(3):
        memory.add_note("thesis", f"why {i}", symbol=f"S{i}")
    memory.ui_set(Memory.INDICATORS_KEY, "rsi")
    (tmp_path / "thndr.ticket.toml").write_text("x = 1\n", encoding="utf-8")
    today = date(2026, 9, 29)
    archive = Archive({"A.CA": _rows(today - timedelta(days=300), [10.0] * 300)})
    e = cur.gather(memory, archive, universe=["A.CA"], config_dir=tmp_path, today=today)
    s = by_key(cur.evaluate(e))
    assert s["prices"].state == "done"          # the whole (one-stock) list is priced
    assert s["preferences"].state == "done" and s["theses"].state == "done"
    assert s["indicators"].state == "done" and s["ticket"].state == "done"
    assert s["lab_runs"].state == "learning" and s["picks"].state == "learning"


def test_a_synthetic_lab_run_is_not_real_evidence(tmp_path):
    memory = Memory(tmp_path / "m.db")
    memory.record_lab_run("k", "rsi", 500, True, True, "passed", "{}")
    e = cur.gather(memory, config_dir=tmp_path)
    assert e.lab_real == 0 and e.lab_passed == 0
    memory.record_lab_run("k2", "rsi", 500, False, True, "passed", "{}")
    e = cur.gather(memory, config_dir=tmp_path)
    assert e.lab_real == 1 and e.lab_passed == 1


def _track(tmp_path, move: float, right_every: int = 1):
    """30 buy picks at 10, each followed by 25 sessions ending at 10 * (1 + move)."""
    memory = Memory(tmp_path / "m.db")
    start = date(2026, 1, 1)
    paths = {}
    picks = []
    for i in range(cur.TRACK_PICKS):
        symbol = f"S{i}.CA"
        end = 10 * (1 + move / 100) if i % right_every == 0 else 10 * (1 - move / 100)
        paths[symbol] = _rows(start, [10.0] + [end] * 25)
        picks.append(Pick(start, "scan", symbol, "buy", 10.0, "test"))
    memory.record_picks(picks)
    return cur.gather(memory, Archive(paths), config_dir=tmp_path)


def test_an_edge_counts_only_after_costs_and_with_most_picks_right(tmp_path):
    cost = cur.round_trip_cost_pct()
    assert 0 < cost < 5
    rich = by_key(cur.evaluate(_track(tmp_path / "a", cost + 1)))
    assert rich["measured"].state == "done" and rich["edge_20"].state == "done"
    # Rising, but by less than a buy and a sell cost: no edge.
    thin = by_key(cur.evaluate(_track(tmp_path / "b", cost / 2)))
    assert thin["edge_20"].state == "learning"
    # Half up by a lot, half down by the same: the average is 0, hit rate 50%.
    mixed = by_key(cur.evaluate(_track(tmp_path / "c", 20, right_every=2)))
    assert mixed["edge_20"].state == "learning"
    # Not old enough for the 60-session check.
    assert rich["edge_60"].progress.have < cur.TRACK_PICKS


def test_the_day_learned_is_kept_and_forgotten_when_the_evidence_goes(tmp_path):
    memory = Memory(tmp_path / "m.db")
    memory.add_note("preference", "banks")
    first = cur.evaluate(cur.gather(memory, config_dir=tmp_path))
    days = cur.remember(memory, first, "2026-09-29")
    assert days["preferences"] == "2026-09-29"
    later = cur.evaluate(cur.gather(memory, config_dir=tmp_path))
    assert cur.remember(memory, later, "2026-10-05")["preferences"] == "2026-09-29"
    memory.delete_note(memory.notes()[0].id)
    gone = cur.evaluate(cur.gather(memory, config_dir=tmp_path))
    assert "preferences" not in cur.remember(memory, gone, "2026-10-06")
    assert memory.ui_get(cur.DONE_KEY + "preferences") == ""


# --------------------------------------------------------------------------- tab


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_tab_shows_ticks_from_evidence(app, tmp_path):
    from egx_advisor.desktop.learn_tab import LearnTab, app_remember

    memory = Memory(tmp_path / "m.db")
    memory.add_note("preference", "banks")
    tab = LearnTab(lambda: cur.gather(memory, config_dir=tmp_path), app_remember(memory))
    tab.refresh()
    end = time.monotonic() + 5
    while not tab.statuses and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    done, measured = cur.summary(tab.statuses)
    assert done == 1 and measured == sum(1 for c in cur.CHECKPOINTS if c.check)
    marks = [tab.tree.topLevelItem(i).text(0) for i in range(tab.tree.topLevelItemCount())]
    assert marks.count("✓") == 1 and "—" in marks
    assert tab.bar.value() == 1

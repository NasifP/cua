"""Sprint 4 logic: action cards, alerts, chart marks and the lead's decisions (no Qt)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from egx_advisor import alerts, chart_marks, daily, indicators
from egx_advisor.analyst import committee as cm
from egx_advisor.bus import StateBus
from egx_advisor.clock import CAIRO
from egx_advisor.memory import Pick
from tests.test_analyst import _bars

TODAY = date(2026, 9, 28)


def _snap(**payloads):
    return {k: {"ts": "", "payload": v} for k, v in payloads.items()}


PORTFOLIO = {"cash_egp": "5000", "positions": [
    {"symbol": "COMI.CA", "quantity": "100", "market_value": "7500"},     # 75: under stop 80
    {"symbol": "TMGH.CA", "quantity": "100", "market_value": "12000"}]}   # 120: past T1 110
LEVELS = {"levels": {
    "COMI.CA": {"price": 75, "stop": 80, "target1": 95, "target2": 105, "status": "below_stop"},
    "TMGH.CA": {"price": 120, "stop": 100, "target1": 110, "target2": 130,
                "status": "above_target1"}}}


# --------------------------------------------------------------------------- cards


def test_cards_come_from_the_team_the_plan_and_the_levels():
    snaps = _snap(
        portfolio=PORTFOLIO, levels=LEVELS,
        plan={"orders": [{"symbol": "ETEL.CA", "side": "buy", "quantity": "40",
                          "limit_price": "30", "rationale": "four factors agree"}]},
        team_views={"SWDY": {"decision": "buy", "reason": "RSI breakout + good news",
                             "day": TODAY.isoformat(), "buy_allowed": True, "max_egp": 900},
                    "ORAS": {"decision": "buy", "reason": "old", "day": "2026-09-01"}})
    cards = {c.ticker: c for c in daily.build(snaps, TODAY)}
    assert set(cards) == {"COMI", "TMGH", "ETEL", "SWDY"}   # ORAS's view is stale
    assert cards["COMI"].decision == "sell" and cards["COMI"].source == "levels"
    assert not cards["COMI"].can_prepare and cards["COMI"].blocked == "sell_side"
    assert cards["ETEL"].quantity == 40 and cards["ETEL"].can_prepare
    assert cards["SWDY"].reason.startswith("RSI") and cards["SWDY"].max_egp == 900
    assert [c.decision for c in daily.build(snaps, TODAY)][0] == "sell"  # protect first


def test_the_team_decides_over_the_plan_and_its_gate_holds():
    snaps = _snap(plan={"orders": [{"symbol": "ETEL.CA", "side": "buy", "quantity": "40",
                                    "limit_price": "30", "rationale": "rules"}]},
                  team_views={"ETEL": {"decision": "buy", "reason": "team says wait",
                                       "day": TODAY.isoformat(), "buy_allowed": False}})
    card = daily.build(snaps, TODAY)[0]
    assert card.source == "team" and card.quantity == 40 and card.limit_price == 30
    assert not card.can_prepare and card.blocked == "team_gate"


@pytest.mark.parametrize(("extra", "halted", "blocked"), [
    ({}, True, "halted"),
    ({"regime": {"risk_state": "buys_halted"}}, False, "news_brake"),
    ({"regime": {"risk_state": "risk_on", "blocked_symbols": ["ETEL.CA"]}}, False, "news_brake"),
])
def test_halt_and_the_news_brake_disable_prepare(extra, halted, blocked):
    snaps = _snap(plan={"orders": [{"symbol": "ETEL.CA", "side": "buy", "quantity": "4",
                                    "limit_price": "30"}]}, **extra)
    card = daily.build(snaps, TODAY, halted=halted)[0]
    assert not card.can_prepare and card.blocked == blocked


def test_the_suggested_quantity_respects_cap_cash_and_liquidity():
    card = daily.Card("ETEL", "buy", "x", "team", limit_price=30.0, max_egp=900)
    assert daily.suggested_quantity(card, cash_egp=5000, liquidity_max=100) == 30
    assert daily.suggested_quantity(card, cash_egp=300, liquidity_max=100) == 10
    assert daily.suggested_quantity(card, cash_egp=5000, liquidity_max=0) == 0


# --------------------------------------------------------------------------- alerts


def test_alerts_for_a_stop_a_target_and_the_brake():
    snaps = _snap(portfolio=PORTFOLIO, levels=dict(LEVELS, macro={
        "MACRO_RISK_OFF": True, "reasons": ["EGX30 fell 5.0% in one session"]}))
    found = {(a.kind, a.ticker) for a in alerts.check(snaps)}
    assert found == {("stop", "COMI"), ("target1", "TMGH"), ("brake", "")}
    title, body = alerts.message(next(a for a in alerts.check(snaps) if a.kind == "stop"), "en")
    assert "COMI" in title and "80.00" in body


def test_each_alert_is_sent_once_a_day(tmp_path):
    bus = StateBus(tmp_path / "s.db")
    ledger = alerts.Ledger(bus)
    stop = alerts.Alert("stop", "COMI", 75, 80)
    assert ledger.claim([stop], TODAY) == [stop]
    assert ledger.claim([stop], TODAY) == []          # same day: not again
    assert ledger.claim([stop], date(2026, 9, 29)) == [stop]  # next day: again


# --------------------------------------------------------------------------- chart marks


def test_marks_are_sorted_and_carry_every_source():
    rows = _bars(300)
    picks = [Pick(rows[-10].day, "scan", "COMI.CA", "buy", 80.0, "4/4")]
    marks, lines = chart_marks.build("COMI", rows, choices=indicators.default_set(),
                                     picks=picks, levels={"stop": 80, "target1": 95},
                                     view={"decision": "trim",
                                           "day": date.today().isoformat()})
    assert [m["time"] for m in marks] == sorted(m["time"] for m in marks)
    shapes = {m["shape"] for m in marks}
    assert {"circle", "square", "arrowDown"} <= shapes
    assert marks[-1]["text"] == "Team: trim" and marks[-1]["time"] == rows[-1].day.isoformat()
    assert [l["title"] for l in lines] == ["Stop", "T1"]


def test_the_rules_replay_enters_and_exits():
    closes = [50 + 0.3 * i for i in range(250)] + [125 - 0.4 * i for i in range(120)]
    start = date(2025, 1, 1)
    rows = [SimpleNamespace(day=start + timedelta(days=i), open=c, high=c + 1, low=c - 1,
                            close=c, volume=1000) for i, c in enumerate(closes)]
    sides = [m.side for m in chart_marks.rule_marks(rows)]
    assert sides and sides[0] == "buy" and "sell" in sides


def test_chart_data_cannot_close_the_script_block():
    rows = _bars(5)
    page = chart_marks.page_html(rows, [{"time": "x", "text": "</script><b>"}], [],
                                 legend="<i>")
    assert "</script><b>" not in page and "&lt;i&gt;" in page


# --------------------------------------------------------------------------- decisions


def test_the_leads_decision_lines_are_read_stripped_and_kept(tmp_path):
    text, found = cm.parse_decisions("القرار: شراء.\nDECISION: COMI buy | RSI breakout\n"
                                     "DECISION: nonsense-ticker!! buy")
    assert text == "القرار: شراء.\nDECISION: nonsense-ticker!! buy"
    assert found == [cm.Decision("COMI", "buy", "RSI breakout")]
    bus = StateBus(tmp_path / "s.db")
    gate = cm.Gate(True, 900.0, [], frozenset({"COMI"}))
    cm.save_views(bus, found, gate, now=datetime(2026, 9, 28, 12, tzinfo=CAIRO))
    view = bus.get("team_views")["payload"]["COMI"]
    assert view["decision"] == "buy" and view["buy_allowed"] and view["day"] == "2026-09-28"


def test_an_old_team_view_is_not_drawn_as_todays():
    view = {"decision": "buy", "day": "2026-09-01"}
    assert chart_marks.team_mark(view, "2026-09-29", today="2026-09-29") == []
    assert chart_marks.team_mark(dict(view, day="2026-09-29"), "2026-09-29",
                                 today="2026-09-29")[0].text == "Team: buy"


def test_a_broken_stop_is_not_hidden_by_the_teams_view_and_risk_off_blocks_buys():
    snaps = {"levels": {"payload": {"macro": {"MACRO_RISK_OFF": True}, "levels": {
                 "COMI.CA": {"status": "below_stop", "stop": 80, "price": 78}}}},
             "team_views": {"payload": {
                 "COMI": {"decision": "buy", "day": TODAY.isoformat(), "buy_allowed": True},
                 "ETEL": {"decision": "buy", "day": TODAY.isoformat(), "buy_allowed": True}}}}
    cards = {c.ticker: c for c in daily.build(snaps, TODAY)}
    assert cards["COMI"].decision == "sell" and cards["COMI"].source == "levels"
    assert not cards["ETEL"].can_prepare and cards["ETEL"].blocked == "macro"

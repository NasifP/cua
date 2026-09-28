"""The analyst committee: parallel specialists, a lead, a code-made buy gate, one budget."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

import pytest

from egx_advisor import spend
from egx_advisor.analyst import committee as cm
from egx_advisor.analyst.tools import Toolbox
from egx_advisor.assistant import Assistant
from egx_advisor.bus import StateBus
from egx_advisor.clock import CAIRO

from tests.test_analyst import RSS, _bars

PRICE = (1e-7, 1e-6)  # dollars per input / output token


@pytest.fixture
def bus(tmp_path):
    bus = StateBus(tmp_path / "s.db")
    bus.put("portfolio", {"as_of": "2026-09-27T09:00:00+00:00", "total_value": "20000",
                          "cash_egp": "5000", "positions": [
                              {"symbol": "COMI.CA", "quantity": "100", "market_value": "9000",
                               "avg_cost": "80"}]})
    bus.resume(actor="test")
    return bus


class FakeBrowser:
    def __init__(self):
        self.calls = []

    def call(self, operation, **args):
        self.calls.append((operation, args))
        return {"ok": True, "url": "https://x.thndr.app/stocks/COMI", "buy_buttons_marked": 1}


@pytest.fixture
def toolbox(bus):
    return Toolbox(bus=bus, history=lambda symbols, days: {s: _bars() for s in symbols},
                   fetch_url=lambda url: RSS, browser=FakeBrowser())


def reply(text=None, calls=()):
    message = {"content": text}
    if calls:
        message["tool_calls"] = [{"id": f"c{i}", "function": {"name": n, "arguments":
                                                             json.dumps(a)}}
                                 for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": message}]}


class Script:
    """A fake model: answers by who is asking (the system prompt), records everything."""

    def __init__(self, reports, lead_calls=(), barrier=None):
        self.reports = reports
        self.lead_calls = list(lead_calls)
        self.barrier = barrier
        self.seen = []
        self.lock = threading.Lock()

    def who(self, messages):
        first = messages[0]["content"]
        for member in cm.MEMBERS:
            if first == member.prompt:
                return member.key
        return "lead" if first == cm.LEAD_PROMPT else "?"

    def __call__(self, **kwargs):
        who = self.who(kwargs["messages"])
        with self.lock:
            self.seen.append((who, kwargs))
        rounds = sum(1 for w, _ in self.seen if w == who)
        if who in self.reports:
            if rounds == 1:
                if self.barrier is not None:
                    self.barrier.wait(timeout=5)  # all three must be in flight together
                tool = {"technical": ("analyze_stock", {"symbol": "COMI"}),
                        "news": ("search_news", {"query": "CIB"}),
                        "risk": ("get_portfolio", {})}[who]
                # Each also tries a tool that is not theirs.
                return reply(calls=[tool, ("prepare_buy", {"symbol": "COMI", "quantity": 1,
                                                           "limit_price": 90})])
            return reply(self.reports[who])
        if who == "lead" and rounds == 1 and self.lead_calls:
            return reply(calls=self.lead_calls)
        return reply("القرار: شراء. الأسباب... الوقف 85 والهدف 100.")


GOOD = {"technical": "اتجاه صاعد.\nVERDICT: positive",
        "news": "أخبار كويسة.\nVERDICT: positive\nBRAKE: no",
        "risk": "كاش كفاية.\nRISK: reduce\nMAX_EGP: 1,000"}


def analyzer(toolbox, bus, script, **kwargs):
    return cm.MultiAgentAnalyzer(toolbox, script, worker_model="cheap", lead_model="strong",
                                 bus=bus, env={"EGX_DAILY_SPEND_LIMIT_EGP": "100"},
                                 price=lambda m: PRICE, cost=lambda r, m: 0.001, **kwargs)


# --------------------------------------------------------------------------- #
# Reports and the gate
# --------------------------------------------------------------------------- #


def test_reports_are_read_from_their_last_lines():
    risk = cm.parse_report(cm.MEMBERS[2], "blah RISK: allow ... RISK: reduce\nMAX_EGP: ٢٬٥٠٠")
    assert risk.ok and risk.risk == "reduce" and risk.max_egp == 2500
    assert not cm.parse_report(cm.MEMBERS[1], "VERDICT: positive").ok  # BRAKE missing
    assert not cm.parse_report(cm.MEMBERS[0], "", error="boom").ok


def _reports(**texts):
    base = dict(GOOD, **texts)
    return {m.key: cm.parse_report(m, base[m.key]) for m in cm.MEMBERS}


@pytest.mark.parametrize(("change", "reason"), [
    ({"news": "VERDICT: negative\nBRAKE: yes"}, "فرامل"),
    ({"risk": "RISK: reject\nMAX_EGP: none"}, "رفض"),
    ({"risk": "RISK: reduce\nMAX_EGP: none"}, "مبلغ"),
    ({"technical": "no verdict line"}, "ناقص"),
])
def test_risk_or_news_warnings_or_a_missing_report_close_the_buy_gate(change, reason):
    gate = cm.decide(_reports(**change))
    assert not gate.buy_allowed and any(reason in r for r in gate.reasons)


def test_a_negative_technical_view_alone_leaves_the_decision_to_the_lead():
    gate = cm.decide(_reports(technical="VERDICT: negative"))
    assert gate.buy_allowed and gate.max_egp == 1000


def test_the_briefing_carries_every_report_and_the_gate():
    text = cm.briefing(_reports(), cm.decide(_reports()))
    assert all(m.title in text for m in cm.MEMBERS) and "1,000" in text


# --------------------------------------------------------------------------- #
# Worst case
# --------------------------------------------------------------------------- #


def test_the_worst_case_grows_with_rounds_and_bounds_output():
    t_in, t_out, calls = cm.worst_case_tokens(1000, steps=2, calls_per_step=3, max_tokens=500)
    grow = 3 * cm.TOOL_RESULT_CHARS + 500
    assert calls == 3 and t_out == 1500
    assert t_in == 1001 * 3 + grow * (0 + 1 + 2)


def test_an_unpriced_model_reserves_calls_but_no_money(toolbox, bus):
    a = cm.MultiAgentAnalyzer(toolbox, Script(GOOD), worker_model="x", lead_model="y", bus=bus,
                              price=lambda m: None)
    usd, calls = a.estimate("q", (), (), ())
    assert usd is None and calls == 3 * (a.worker_steps + 1) + a.lead_steps + 1


# --------------------------------------------------------------------------- #
# Reservations in the ledger
# --------------------------------------------------------------------------- #

ENV = {"EGX_DAILY_SPEND_LIMIT_EGP": "10", "EGX_DAILY_CALL_LIMIT": "10"}  # $0.20 at 50


def test_a_reservation_that_does_not_fit_is_refused(bus):
    with pytest.raises(spend.BudgetExceeded):
        spend.reserve(bus, usd=0.25, calls=1, purpose="t", env=ENV)
    with pytest.raises(spend.BudgetExceeded):
        spend.reserve(bus, usd=0.01, calls=11, purpose="t", env=ENV)
    assert spend.today(bus)["reservations"] == {}


def test_others_cannot_spend_what_is_reserved_and_the_holder_can(bus):
    held = spend.reserve(bus, usd=0.19, calls=2, purpose="t", env=ENV)
    plain = spend.metered(lambda **k: "r", purpose="other", bus_factory=lambda: bus, env=ENV,
                          cost=lambda r, m: 0.02)
    plain()  # 0.00 spent + 0.19 held < 0.20: allowed, and now 0.21 committed
    with pytest.raises(spend.BudgetExceeded):
        plain()
    mine = spend.metered(lambda **k: "r", purpose="mine", bus_factory=lambda: bus, env=ENV,
                         cost=lambda r, m: 0.05, reservation=held)
    mine()
    live = spend.today(bus)["reservations"][held.id]
    assert live["calls"] == 1 and live["usd"] == pytest.approx(0.14)
    spend.release(bus, held)
    totals = spend.today(bus)
    assert totals["reservations"] == {} and totals["usd"] == pytest.approx(0.07)


def test_a_reservation_left_by_a_crash_expires(bus):
    held = spend.reserve(bus, usd=0.19, calls=2, purpose="t", env=ENV)
    later = datetime.now(CAIRO) + spend.RESERVATION_TTL + timedelta(seconds=5)
    assert held.id not in spend.today(bus, later)["reservations"]


# --------------------------------------------------------------------------- #
# The whole committee
# --------------------------------------------------------------------------- #


def test_three_specialists_run_at_once_then_the_lead_decides(toolbox, bus):
    script = Script(GOOD, barrier=threading.Barrier(3))
    result = analyzer(toolbox, bus, script).run("عايز أشتري COMI")
    assert {w for w, _ in script.seen} == {"technical", "news", "risk", "lead"}
    lead = [k for w, k in script.seen if w == "lead"][0]
    briefing = "\n".join(m["content"] for m in lead["messages"] if m["role"] == "system")
    assert "اتجاه صاعد" in briefing and "RISK: reduce" in briefing
    assert "prepare_buy" in [t["function"]["name"] for t in lead["tools"]]
    assert cm.DISCLAIMER in result.text and "فريق التحليل" in result.text


def test_each_specialist_gets_only_its_own_tools(toolbox, bus):
    script = Script(GOOD)
    analyzer(toolbox, bus, script).run("COMI")
    for member in cm.MEMBERS:
        first = next(k for w, k in script.seen if w == member.key)
        assert {t["function"]["name"] for t in first["tools"]} <= set(member.tools)
        second = [k for w, k in script.seen if w == member.key][1]
        refused = [m for m in second["messages"] if m["role"] == "tool"
                   and m["name"] == "prepare_buy"]
        assert "not one of your tools" in refused[0]["content"]
        assert all(len(m["content"]) <= cm.TOOL_RESULT_CHARS
                   for m in second["messages"] if m["role"] == "tool")
    assert bus.get("ticket_proposal") is None, "a specialist prepared an order"


def test_a_risk_rejection_takes_prepare_buy_away_from_the_lead(toolbox, bus):
    script = Script(dict(GOOD, risk="RISK: reject\nMAX_EGP: none"),
                    lead_calls=[("prepare_buy", {"symbol": "COMI", "quantity": 5,
                                                 "limit_price": 90})])
    result = analyzer(toolbox, bus, script).run("اشتري COMI")
    lead = [k for w, k in script.seen if w == "lead"]
    assert "prepare_buy" not in [t["function"]["name"] for t in lead[0]["tools"]]
    assert bus.get("ticket_proposal") is None and not result.gate.buy_allowed
    assert "تجهيز الشراء مقفول" in result.text


def test_the_risk_managers_amount_caps_a_prepared_buy(toolbox, bus):
    too_big = [("prepare_buy", {"symbol": "COMI", "quantity": 20, "limit_price": 90})]
    script = Script(GOOD, lead_calls=too_big)
    analyzer(toolbox, bus, script).run("اشتري COMI")
    tool_msg = [m for m in [k for w, k in script.seen if w == "lead"][1]["messages"]
                if m["role"] == "tool"][0]
    assert "capped" in tool_msg["content"] and bus.get("ticket_proposal") is None
    fits = [("prepare_buy", {"symbol": "COMI", "quantity": 10, "limit_price": 90})]
    analyzer(toolbox, bus, Script(GOOD, lead_calls=fits)).run("اشتري COMI")
    assert bus.get("ticket_proposal")["payload"]["quantity"] == "10"


def test_all_calls_are_counted_once_and_the_reservation_is_released(toolbox, bus):
    result = analyzer(toolbox, bus, Script(GOOD)).run("COMI")
    totals = spend.today(bus)
    assert result.cost.calls == 7  # 2 rounds for each specialist, 1 for the lead
    assert totals["calls"] == 7 and totals["usd"] == pytest.approx(0.007)
    assert set(totals["by_purpose"]) == {"committee.technical", "committee.news",
                                         "committee.risk", "committee.lead"}
    assert totals["reservations"] == {}
    assert "تكلفة الإجابة: 0.35 ج" in result.text  # 0.007 dollars at 50 EGP


def test_nothing_is_called_when_the_worst_case_does_not_fit(toolbox, bus):
    script = Script(GOOD)
    a = cm.MultiAgentAnalyzer(toolbox, script, worker_model="c", lead_model="s", bus=bus,
                              env={"EGX_DAILY_SPEND_LIMIT_EGP": "0.5"}, price=lambda m: PRICE)
    with pytest.raises(spend.BudgetExceeded):
        a.run("COMI")
    assert script.seen == [] and spend.today(bus)["reservations"] == {}


def test_a_failing_specialist_closes_the_gate_but_the_answer_comes(toolbox, bus):
    script = Script(GOOD)
    real = script.__call__

    def flaky(**kwargs):
        if script.who(kwargs["messages"]) == "news":
            raise RuntimeError("429 quota")
        return real(**kwargs)

    result = analyzer(toolbox, bus, flaky).run("COMI")
    assert not result.gate.buy_allowed and result.reports["news"].error
    assert "أخبار: غير متاح" in result.text


def test_the_chat_uses_the_committee_when_switched_on(toolbox, bus, monkeypatch):
    monkeypatch.setattr(spend, "price", lambda m: PRICE)
    monkeypatch.setattr(cm.spend, "price", lambda m: PRICE)
    script = Script(GOOD)
    assistant = Assistant(bus=bus, model="strong", completion=lambda **k: reply("single"),
                          toolbox=toolbox, committee=True, worker_model="cheap",
                          raw_completion=script)
    text = assistant.answer("حلل COMI")
    assert "فريق التحليل" in text
    assert {k["model"] for w, k in script.seen if w != "lead"} == {"cheap"}
    assert {k["model"] for w, k in script.seen if w == "lead"} == {"strong"}

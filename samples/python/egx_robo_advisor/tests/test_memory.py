"""Memory: notes, conversation, lab journal, picks, the price archive and the review."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal

import pytest

from egx_advisor import memory as mem
from egx_advisor.assistant import Assistant
from egx_advisor.bus import StateBus
from egx_advisor.marketdata import MarketDataError, YahooRow
from egx_advisor.marketdata.archive import ArchivedMarketData, PriceArchive
from egx_advisor.memory import Memory, Pick
from egx_advisor.review import describe, measure, review, scorecard
from egx_advisor.types import Instrument, Sleeve

D = Decimal


@pytest.fixture
def memory(tmp_path):
    return Memory(tmp_path / "memory" / "memory.db")


def test_notes_are_added_edited_and_deleted(memory):
    note_id = memory.add_note("thesis", "Why: dividends\nExit if: payout cut", symbol="comi")
    memory.add_note("preference", "I prefer dividend stocks")
    assert [n.symbol for n in memory.notes("thesis")] == ["COMI.CA"]
    memory.update_note(note_id, "thesis", "Why: dividends and growth", symbol="COMI")
    assert memory.notes("thesis")[0].text == "Why: dividends and growth"
    memory.delete_note(note_id)
    assert [n.kind for n in memory.notes()] == ["preference"]
    with pytest.raises(ValueError):
        memory.add_note("order", "buy everything")
    with pytest.raises(ValueError):
        memory.add_note("note", "   ")


def test_the_conversation_is_kept_and_can_be_forgotten(memory):
    for i in range(mem.KEEP_EXCHANGES + 5):
        memory.add_exchange(f"q{i}", f"a{i}")
    talk = memory.recent_exchanges(2)
    assert [e.question for e in talk] == [f"q{mem.KEEP_EXCHANGES + 3}", f"q{mem.KEEP_EXCHANGES + 4}"]
    assert len(memory.recent_exchanges(1000)) == mem.KEEP_EXCHANGES
    memory.clear_conversation()
    assert memory.recent_exchanges() == []


def test_a_pick_is_kept_once_a_day(memory):
    pick = Pick(date(2026, 9, 1), "scan", "COMI", "buy", 80.0, "4/4 factors agree")
    assert memory.record_picks([pick, pick]) == 1
    assert memory.record_picks([Pick(date(2026, 9, 2), "scan", "COMI.CA", "buy", 81.0, "")]) == 1
    assert memory.record_picks([Pick(date(2026, 9, 2), "plan", "X", "buy", 0, "")]) == 0
    assert [p.symbol for p in memory.picks()] == ["COMI.CA", "COMI.CA"]


def test_the_context_puts_preferences_and_named_stocks_first(memory):
    for i in range(40):
        memory.add_note("note", f"filler {i}")
    memory.add_note("thesis", "exit if payout is cut", symbol="COMI")
    memory.add_note("preference", "I prefer dividend stocks")
    memory.record_lab_run("k", "rsi 30 (5y)", 1200, False, False, "noise", "+0.1%/yr")
    block = memory.context("what about COMI?")
    lines = block.splitlines()
    assert lines[2] == "- [preference] I prefer dividend stocks"
    assert lines[3] == "- [thesis COMI.CA] exit if payout is cut"
    assert "<lab_journal>" in block and "rsi 30 (5y): noise" in block
    assert "<earlier_conversation>" not in block


@pytest.mark.parametrize(("question", "expected"), [
    ("افتكر إن أنا بحب أسهم التوزيعات", ("preference", "", "أنا بحب أسهم التوزيعات")),
    ("remember that I bought COMI for the dividend", ("note", "COMI.CA",
                                                      "I bought COMI for the dividend")),
    ("احفظ: هبيع SWDY لو نزل تحت 60", ("note", "SWDY.CA", "هبيع SWDY لو نزل تحت 60")),
    ("ليه البوت وقف؟", None),
    ("why did the bot not remember the plan?", None),
    ("افتكر", None),
])
def test_remember_requests_are_recognised(question, expected):
    assert mem.remember_request(question) == expected


def test_tickers_are_found_without_everyday_words():
    assert mem.mentioned_symbols("why was AZG.CA not bought, and COMI?") == {"AZG.CA", "COMI.CA"}
    assert mem.mentioned_symbols("what does the RSI say about EGX") == set()
    assert mem.mentioned_symbols("and comi?", {"COMI.CA"}) == {"COMI.CA"}


# ------------------------------------------------------------------ the chat


def _chat(tmp_path, memory, **kwargs):
    return Assistant(bus=StateBus(tmp_path / "s.db"), memory=memory, **kwargs)


def test_remember_that_is_saved_by_code_without_the_model(tmp_path, memory):
    assistant = _chat(tmp_path, memory, completion=lambda **_: pytest.fail("model called"))
    answer = assistant.answer("افتكر إن أنا بحب أسهم التوزيعات")
    assert "حفظتها" in answer
    assert memory.notes("preference")[0].source == "chat"


def test_the_model_gets_memory_and_the_exchange_is_kept(tmp_path, memory):
    memory.add_note("preference", "I prefer dividend stocks")
    memory.add_exchange("earlier question", "earlier answer")
    seen = {}

    def completion(**kwargs):
        seen["messages"] = kwargs["messages"]
        return {"choices": [{"message": {"content": "noted"}}]}

    assistant = _chat(tmp_path, memory, completion=completion)
    assert assistant.answer("what should I watch?") == "noted"
    block = next(m["content"] for m in seen["messages"]
                 if m["content"].startswith("<operator_memory>"))
    assert "I prefer dividend stocks" in block and "earlier question" in block
    assert memory.recent_exchanges(1)[0].question == "what should I watch?"

    # With history in the page, the earlier conversation is not repeated.
    assistant.answer("and then?", history=[{"role": "user", "content": "x"}])
    block = next(m["content"] for m in seen["messages"]
                 if m["content"].startswith("<operator_memory>"))
    assert "<earlier_conversation>" not in block


def test_scan_picks_are_kept_for_the_review(tmp_path, memory):
    from tests.test_scanner import _result

    assistant = _chat(tmp_path, memory, use_model=False, scanner=lambda n: _result())
    assistant.answer("ابحث عن افضل 5 فرص")
    [pick] = memory.picks("scan")
    assert (pick.symbol, pick.side, pick.price) == ("COMI.CA", "buy", 80.5)


def test_picks_from_archived_prices_are_not_kept(tmp_path, memory):
    from dataclasses import replace

    from tests.test_scanner import _result

    assistant = _chat(tmp_path, memory, use_model=False,
                      scanner=lambda n: replace(_result(), stale=True))
    assert "أرشيف" in assistant.answer("ابحث عن افضل 5 فرص")
    assert memory.picks() == []


# ------------------------------------------------------------------ archive


def _rows(symbol_close, days=5, end=date(2026, 9, 24)):
    return [YahooRow(end - timedelta(days=days - 1 - i), D(symbol_close), D(symbol_close + 1),
                     D(symbol_close - 1), D(symbol_close + i), D(1000)) for i in range(days)]


class FakeYahoo:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.calls = rows or {}, fail, 0
        self.last_report = None

    async def history(self, universe, days):
        self.calls += 1
        if self.fail:
            raise MarketDataError("yahoo down")
        return self.rows


UNIVERSE = [Instrument("COMI.CA", "COMI.CA", Sleeve.BLUE_CHIP)]


def test_the_archive_keeps_prices_and_serves_a_second_scan_the_same_day(tmp_path):
    archive = PriceArchive(tmp_path / "prices.db")
    yahoo = FakeYahoo({"COMI.CA": _rows(80)})
    source = ArchivedMarketData(yahoo, archive, today=lambda: date(2026, 9, 25))
    asyncio.run(source.history(UNIVERSE, days=30))
    rows = asyncio.run(source.history(UNIVERSE, days=30))
    assert yahoo.calls == 1, "the second read the same day comes from the archive"
    assert [r.close for r in rows["COMI.CA"]] == [D(80), D(81), D(82), D(83), D(84)]
    assert not source.stale


def test_when_yahoo_is_down_the_archive_answers_and_says_so(tmp_path):
    archive = PriceArchive(tmp_path / "prices.db")
    archive.store({"COMI.CA": _rows(80)}, date(2026, 9, 20), date(2026, 8, 1))
    source = ArchivedMarketData(FakeYahoo(fail=True), archive, today=lambda: date(2026, 9, 25))
    rows = asyncio.run(source.history(UNIVERSE, days=30))
    assert len(rows["COMI.CA"]) == 5 and source.stale
    assert source.last_report is not None

    empty = ArchivedMarketData(FakeYahoo(fail=True), PriceArchive(tmp_path / "empty.db"))
    with pytest.raises(MarketDataError):
        asyncio.run(empty.history(UNIVERSE, days=30))


# ------------------------------------------------------------------ review


def test_a_pick_is_measured_after_5_20_and_60_sessions():
    pick_day = date(2026, 1, 1)
    closes = [(pick_day + timedelta(days=i), 100.0 + i) for i in range(0, 30)]
    result = measure(Pick(pick_day, "scan", "COMI.CA", "buy", 100.0, ""), closes)
    assert result.moves[5] == pytest.approx(5.0)
    assert result.moves[20] == pytest.approx(20.0)
    assert result.moves[60] is None, "not reached yet: blank, never guessed"
    sell = measure(Pick(pick_day, "plan", "COMI.CA", "sell", 100.0, ""), closes)
    assert sell.moves[5] == pytest.approx(-5.0), "a sell is right when the price falls"


def test_the_scorecard_counts_only_reached_horizons():
    day = date(2026, 1, 1)
    picks = [Pick(day, "scan", "UP.CA", "buy", 100.0, ""), Pick(day, "scan", "DOWN.CA", "buy",
                                                                 100.0, "")]
    closes = {"UP.CA": [(day + timedelta(days=i), 100.0 + i) for i in range(10)],
              "DOWN.CA": [(day + timedelta(days=i), 100.0 - i) for i in range(10)]}
    scores = scorecard(review(picks, lambda s: closes[s]))
    [five] = scores
    assert (five.source, five.horizon, five.count, five.right) == ("scan", 5, 2, 1)
    assert five.average == pytest.approx(0.0)
    assert "right 1 of 2" in describe(scores, arabic=False)
    assert "5 جلسات" in describe([], arabic=True)

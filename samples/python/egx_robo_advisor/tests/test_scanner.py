"""The opportunity scan: factor maths, ranking, chat intent, and the chat route."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from egx_advisor import scanner
from egx_advisor.assistant import Assistant
from egx_advisor.bus import StateBus
from egx_advisor.marketdata import YahooRow


def _rows(closes, volumes=None, end=date(2026, 9, 24)):
    volumes = volumes or [1000] * len(closes)
    start = end - timedelta(days=len(closes) - 1)
    return [
        YahooRow(day=start + timedelta(days=i), open=Decimal(str(c)), high=Decimal(str(c)),
                 low=Decimal(str(c)), close=Decimal(str(c)), volume=Decimal(str(v)))
        for i, (c, v) in enumerate(zip(closes, volumes, strict=True))
    ]


def _rising(n=150, step=0.002, wobble=0.004):
    prices, p = [], 50.0
    for i in range(n):
        p *= 1 + step + (wobble if i % 2 else -wobble)
        prices.append(round(p, 4))
    return prices


def test_a_steady_riser_with_rising_volume_has_all_four_factors():
    closes = [Decimal(str(c)) for c in _rising()]
    volumes = [Decimal(1000)] * 145 + [Decimal(3000)] * 5
    o = scanner.analyze("UP.CA", closes, volumes)
    assert o is not None
    assert o.agree == {"trend": True, "momentum": True, "volume": True, "volatility": True}
    assert o.momentum_60 > 0 and o.trend_pct > 0


def test_a_faller_fails_trend_and_momentum():
    closes = [Decimal(str(c)) for c in reversed(_rising())]
    o = scanner.analyze("DOWN.CA", closes, [Decimal(1000)] * len(closes))
    assert o is not None and not o.agree["trend"] and not o.agree["momentum"]


def test_too_little_history_is_not_analysed():
    assert scanner.analyze("NEW.CA", [Decimal(10)] * 50, [Decimal(1)] * 50) is None


def test_ranking_puts_agreement_first_then_return_per_risk():
    def opp(symbol, agree, m60, vol):
        flags = dict(zip(("trend", "momentum", "volume", "volatility"),
                         [True] * agree + [False] * (4 - agree), strict=True))
        return scanner.Opportunity(symbol, 10, 1, 1, m60, 100, vol, 50, flags)

    ranked = scanner.rank([opp("A", 3, 50, 1), opp("B", 4, 5, 2), opp("C", 4, 10, 1)], top=2)
    assert [o.symbol for o in ranked] == ["C", "B"]


class FakeYahoo:
    def __init__(self, rows):
        self.rows = rows
        self.last_report = None

    async def history(self, universe, days):
        return self.rows


def test_scan_ranks_and_lists_what_it_could_not_price():
    up = _rows(_rising(), [1000] * 145 + [3000] * 5)
    down = _rows(list(reversed(_rising())))
    result = pytest.importorskip("asyncio").run(scanner.scan(
        FakeYahoo({"UP.CA": up, "DOWN.CA": down}), ["DOWN.CA", "UP.CA", "GONE.CA"], top=5))
    assert [o.symbol for o in result.top] == ["UP.CA", "DOWN.CA"]
    assert result.skipped == ("GONE.CA",)
    table = result.table()
    assert "1. UP.CA" in table and "4/4 agree" in table and "GONE.CA" in table


def test_the_scan_list_ships_and_loads(tmp_path):
    from egx_advisor.paths import RESOURCE_ROOT

    symbols = scanner.load_universe(RESOURCE_ROOT / "config" / "scan_universe.toml")
    assert "COMI.CA" in symbols and len(symbols) == len(set(symbols)) >= 20
    (tmp_path / "u.toml").write_text('symbols = ["comi", "COMI.CA", "etel.ca"]')
    assert scanner.load_universe(tmp_path / "u.toml") == ["COMI.CA", "ETEL.CA"]


@pytest.mark.parametrize(("question", "top"), [
    ("احسن صفقة للدخول بمبلغ 5 الاف جنية؟", 5),
    ("ابحث عن افضل 3 فرص بـ 20000 جنيه", 3),
    ("top 3 stocks for 10k", 3),
    ("ابحث عن افضل 5 فرص للتداول", 5),
    ("ابحث عن أفضل ٣ فرص", 3),
    ("هاتلي احسن اسهم", 5),
    ("find the best 7 trading opportunities", 7),
    ("top 50 stocks", scanner.MAX_TOP),
    ("Why was AZG.CA not bought?", None),
    ("هل في فرصة البوت يبيع؟", None),
    ("ابحث عن سبب إيقاف الشراء", None),
    ("what does the turnover cap do", None),
])
def test_scan_requests_are_recognised(question, top):
    assert scanner.scan_request(question) == top


def _result():
    flags = {"trend": True, "momentum": True, "volume": False, "volatility": True}
    o = scanner.Opportunity("COMI.CA", 80.5, 4.2, 3.1, 9.8, 90, 1.6, 61, flags)
    return scanner.ScanResult(top=(o,), scanned=30, skipped=("XYZ.CA",), as_of="2026-09-25")


def test_the_chat_hands_the_ranked_table_to_the_model(tmp_path):
    seen = {}

    def completion(**kwargs):
        seen["messages"] = kwargs["messages"]
        return {"choices": [{"message": {"content": "COMI.CA leads."}}]}

    asked = []
    assistant = Assistant(bus=StateBus(tmp_path / "s.db"), completion=completion,
                          scanner=lambda n: asked.append(n) or _result())
    assert assistant.answer("ابحث عن افضل 3 فرص") == "COMI.CA leads."
    assert asked == [3]
    block = next(m["content"] for m in seen["messages"]
                 if m["content"].startswith("<scan_results>"))
    assert "1. COMI.CA" in block and "3/4 agree" in block


def test_with_chat_off_a_scan_still_answers_with_figures(tmp_path):
    assistant = Assistant(bus=StateBus(tmp_path / "s.db"), use_model=False,
                          completion=lambda **_: pytest.fail("model called"),
                          scanner=lambda n: _result())
    answer = assistant.answer("ابحث عن افضل 5 فرص")
    assert "نتيجة الفحص" in answer and "1. COMI:" in answer and "3 من 4" in answer


def test_a_failed_scan_is_reported_not_raised(tmp_path):
    def broken(_n):
        raise RuntimeError("yahoo down")

    assistant = Assistant(bus=StateBus(tmp_path / "s.db"), use_model=False, scanner=broken)
    assert "yahoo down" in assistant.answer("find the best 5 opportunities")


def test_other_questions_never_run_the_scan(tmp_path):
    assistant = Assistant(
        bus=StateBus(tmp_path / "s.db"),
        completion=lambda **_: {"choices": [{"message": {"content": "ok"}}]},
        scanner=lambda n: pytest.fail("scanned"))
    assert assistant.answer("why is buying halted?") == "ok"


@pytest.mark.parametrize(("question", "amount"), [
    ("احسن صفقة للدخول بمبلغ 5 الاف جنية؟", 5000),
    ("ابحث عن افضل ٣ فرص بـ ٢٠٠٠٠ جنيه", 20000),
    ("best 5 trades with EGP 10,000", 10000),
    ("top 3 stocks for 10k", 10000),
    ("ابحث عن أفضل 5 فرص", None),
])
def test_an_amount_in_the_question_is_read_in_pounds(question, amount):
    assert scanner.scan_budget(question) == amount


def test_the_amount_becomes_whole_shares_at_the_close():
    result = _result()
    assert "buys 62 shares" in result.table(5000)
    assert "بمبلغ 5,000 ج.م: 62 سهم" in result.summary(arabic=True, budget=5000)


def test_a_quota_error_is_one_readable_line_and_the_scan_still_shows(tmp_path):
    def over_quota(**_):
        raise RuntimeError('litellm.RateLimitError: geminiException - {"error": {"code": 429, '
                           '"message": "You exceeded your current quota"}}')

    assistant = Assistant(bus=StateBus(tmp_path / "s.db"), model="gemini/x-pro",
                          completion=over_quota, scanner=lambda n: _result())
    answer = assistant.answer("احسن صفقة للدخول بمبلغ 5 الاف جنية؟")
    assert "gemini/x-pro" in answer and "gemini-2.5-flash" in answer
    assert "{" not in answer, "no provider JSON in the chat"
    assert "1. COMI:" in answer and "62 سهم" in answer

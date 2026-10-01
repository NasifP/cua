"""The analyst's hands in Thndr X: what it may click, where it may go, what it prepares."""

from __future__ import annotations

import json
import time
import os
import urllib.error
import urllib.request

import pytest

from egx_advisor import browse

HOME = "https://x.thndr.app"


# --------------------------------------------------------------------------- #
# Words
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text", [
    "Buy", "BUY NOW", "Sell", "Place order", "Confirm", "Submit", "Review order",
    "Deposit", "Withdraw", "Log out", "Cancel", "Next", "Continue",
    "شراء", "اشتري", "بيع", "بيع الكل", "تأكيد الأمر", "أكّد", "إرسال", "تنفيذ",
    "إيداع", "سحب", "تسجيل الخروج", "إلغاء", "الأوامر", "ـشـراء",  # tatweel too
])
def test_order_and_money_words_are_never_clickable(text):
    assert browse.forbidden(text), text


@pytest.mark.parametrize("text", [
    "Overview", "News", "Financials", "Buyback news", "COMI", "Watchlist", "Markets",
    "Portfolio", "نظرة عامة", "الأخبار", "القوائم المالية", "البنك التجاري الدولي", "المحفظة",
    "قائمة المتابعة",
])
def test_ordinary_tabs_and_names_are_clickable(text):
    assert browse.forbidden(text) is None, text


# --------------------------------------------------------------------------- #
# Places
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("url", "ok"), [
    ("https://x.thndr.app/workspaces/default/home", True),
    ("http://x.thndr.app/", False),
    ("https://thndr.app/", False),
    ("https://x.thndr.app.evil.example/", False),
    ("https://user:pw@x.thndr.app/", False),
    ("javascript:alert(1)", False),
    ("", False),
])
def test_only_the_thndr_x_host_is_allowed(url, ok):
    assert browse.allowed_url(url, HOME) is ok


def test_a_path_resolves_on_the_home_host():
    assert browse.resolve("/workspaces/default/home", HOME) == \
        "https://x.thndr.app/workspaces/default/home"


def test_a_stock_address_is_learned_from_one_ticker_segment():
    url = "https://x.thndr.app/workspaces/default/stocks/COMI/overview"
    pattern = browse.stock_pattern(url, ["COMI", "ETEL"], HOME)
    assert pattern == "https://x.thndr.app/workspaces/default/stocks/{ticker}/overview"
    assert browse.stock_url(pattern, "ETEL").endswith("/stocks/ETEL/overview")
    assert browse.stock_pattern("https://x.thndr.app/home", ["COMI"], HOME) is None
    assert browse.stock_pattern("https://evil.example/stocks/COMI", ["COMI"], HOME) is None


@pytest.mark.parametrize(("symbol", "ticker"), [
    ("COMI.CA", "COMI"), (" comi ", "COMI"), ("ETEL", "ETEL"), ("../x", ""), ("", ""),
    ("A", ""), ("COMI; DROP", ""),
])
def test_tickers_are_checked(symbol, ticker):
    assert browse.ticker_of(symbol) == ticker


# --------------------------------------------------------------------------- #
# The operations, over a fake page
# --------------------------------------------------------------------------- #


class FakePage:
    def __init__(self, url=HOME + "/workspaces/default/home", text="Home", labels=()):
        self.url, self.text, self.labels = url, text, list(labels)
        self.scripts: list[str] = []
        self.loads: list[str] = []
        self.click_result = {"ok": True, "clicked": "News"}
        self.search_results: list[str] = []
        self.on_click = None

    def info(self):
        return {"url": self.url, "title": "Thndr", "loading": False}

    def run(self, script):
        self.scripts.append(script)
        if "clickables()" in script and "want" in script:
            if self.on_click:
                self.on_click()
            return json.dumps(self.click_result)
        if "SEARCHY" in script:
            self.labels = self.search_results
            return json.dumps({"ok": True})
        if "marked" in script:
            return json.dumps({"ok": True, "marked": 1})
        return json.dumps({"ok": True, "text": self.text, "labels": self.labels})

    def load(self, url):
        self.loads.append(url)
        self.url = url


def make(page=None, halted=False, on=True, memory=None, **kwargs):
    memory = {} if memory is None else memory
    logs: list[str] = []
    b = browse.Browser(page or FakePage(), HOME, is_halted=lambda: halted,
                       is_enabled=lambda: on, remember=memory.__setitem__, recall=memory.get,
                       known_tickers=lambda: ["COMI", "ETEL"], log=logs.append,
                       settle=0, sleep=lambda s: None, **kwargs)
    return b, memory, logs


def test_reading_drops_order_buttons_from_what_the_model_is_offered():
    page = FakePage(labels=["Overview", "News", "Buy", "شراء", "Sell"])
    b, _, _ = make(page)
    assert b.read()["clickable"] == ["Overview", "News"]


def test_a_forbidden_click_is_refused_before_the_page_is_touched():
    page = FakePage()
    b, _, _ = make(page)
    for text in ("Buy", "تأكيد", "Place order"):
        assert "refused" in b.handle("click", {"text": text})
    assert page.scripts == []


def test_the_page_script_refusing_is_reported_and_logged():
    page = FakePage()
    page.click_result = {"ok": False, "reason": "in_ticket", "label": "Max"}
    b, _, logs = make(page)
    out = b.handle("click", {"text": "Max"})
    assert "refused" in out and "in_ticket" in out["refused"]
    assert any(line.startswith("guard:") for line in logs)


@pytest.mark.parametrize(("halted", "on"), [(True, True), (False, False)])
def test_halt_or_the_switch_stops_every_move(halted, on):
    page = FakePage()
    b, _, _ = make(page, halted=halted, on=on)
    for op, args in (("open", {"url": "/x"}), ("click", {"text": "News"}),
                     ("search", {"query": "COMI"}), ("open_stock", {"symbol": "COMI"}),
                     ("prepare", {"symbol": "COMI"})):
        assert "refused" in b.handle(op, args), op
    assert page.loads == [] and page.scripts == []


def test_reading_works_while_halted():
    b, _, _ = make(halted=True)
    assert b.handle("read", {})["title"] == "Thndr"


def test_opening_another_site_is_refused():
    page = FakePage()
    b, _, _ = make(page)
    assert "refused" in b.handle("open", {"url": "https://evil.example/"})
    assert page.loads == []


def test_unknown_operations_and_bad_arguments_are_answers_not_crashes():
    b, _, _ = make()
    assert b.handle("run", {"script": "alert(1)"})["error"].startswith("no such operation")
    assert "bad arguments" in b.handle("click", {"nope": 1})["error"]


def test_open_stock_uses_the_learned_address():
    page = FakePage(text="COMI Commercial International Bank")
    memory = {browse.STOCK_URL_KEY: HOME + "/workspaces/default/stocks/{ticker}"}
    b, _, _ = make(page, memory=memory)
    out = b.handle("open_stock", {"symbol": "comi.ca"})
    assert out["ok"] and out["via"] == "address"
    assert page.loads == [HOME + "/workspaces/default/stocks/COMI"]


def test_open_stock_searches_then_learns_the_address():
    page = FakePage(text="COMI")
    page.search_results = ["COMI Commercial International Bank", "COMI bonds? no"]

    def navigate():
        page.url = HOME + "/workspaces/default/stocks/COMI"
    page.on_click = navigate
    page.click_result = {"ok": True, "clicked": "COMI Commercial International Bank"}
    b, memory, logs = make(page)
    out = b.handle("open_stock", {"symbol": "COMI"})
    assert out["ok"] and out["via"] == "search"
    assert memory[browse.STOCK_URL_KEY] == HOME + "/workspaces/default/stocks/{ticker}"
    assert any("learned" in line for line in logs)


def test_prepare_opens_the_stock_and_only_points_at_buy():
    page = FakePage(text="COMI")
    memory = {browse.STOCK_URL_KEY: HOME + "/stocks/{ticker}"}
    b, _, _ = make(page, memory=memory)
    out = b.handle("prepare", {"symbol": "COMI"})
    assert out["buy_buttons_marked"] == 1
    assert browse.POINT_AT_BUY_SCRIPT in page.scripts
    assert ".click()" not in browse.POINT_AT_BUY_SCRIPT


# --------------------------------------------------------------------------- #
# A proposed buy
# --------------------------------------------------------------------------- #


def propose(**overrides):
    kwargs = dict(symbol="COMI", quantity=10, limit_price=85.5, last_price=85.0,
                  cash_egp=5000.0, halted=False, blocked=())
    kwargs.update(overrides)
    return browse.check_proposal(kwargs.pop("symbol"), kwargs.pop("quantity"),
                                 kwargs.pop("limit_price"), **kwargs)


def test_a_sound_proposal_is_cleaned_up():
    problem, order = propose()
    assert problem is None
    assert order == {"symbol": "COMI.CA", "side": "buy", "quantity": "10",
                     "limit_price": "85.5", "value_egp": "855.0"}


@pytest.mark.parametrize(("overrides", "word"), [
    ({"symbol": "../etc"}, "ticker"),
    ({"halted": True}, "halted"),
    ({"quantity": 2.5}, "whole"),
    ({"quantity": 0}, "whole"),
    ({"quantity": "ten"}, "numbers"),
    ({"limit_price": -1}, "above zero"),
    ({"limit_price": 100}, "from the last price"),
    ({"quantity": 100}, "cash"),
    ({"blocked": ["COMI.CA"]}, "news brake"),
])
def test_a_bad_proposal_is_refused(overrides, word):
    problem, order = propose(**overrides)
    assert problem and word in problem and order == {}


def test_without_a_reference_price_or_cash_the_proposal_still_needs_sane_numbers():
    problem, _ = propose(last_price=None, cash_egp=None)
    assert problem is None


# --------------------------------------------------------------------------- #
# Server and client
# --------------------------------------------------------------------------- #


def test_the_server_needs_the_secret_and_only_takes_posts():
    b, _, _ = make(FakePage(labels=["News"]))
    server = browse.BrowseServer(b).start()
    try:
        client = browse.BrowseClient.from_env(server.env())
        assert client.call("read")["clickable"] == ["News"]
        assert "refused" in client.call("click", text="Buy")
        wrong = browse.BrowseClient(server.url, "wrong")
        assert "401" in wrong.call("read")["error"]
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(server.url + "/read", timeout=5)
        assert err.value.code == 405
    finally:
        server.stop()


def test_no_client_without_the_environment():
    assert browse.BrowseClient.from_env({}) is None


def test_browsing_is_on_unless_switched_off():
    assert browse.enabled({}) and browse.enabled({"EGX_BROWSE": "true"})
    assert not browse.enabled({"EGX_BROWSE": "false"})


# --------------------------------------------------------------------------- #
# The page scripts in a real Chromium (set EGX_E2E_CHROMIUM to run)
# --------------------------------------------------------------------------- #

FAKE_THNDR = """<!doctype html><html><body>
<nav><a href="/news" id="news">News</a> <a href="https://evil.example/x">Offers</a>
  <div role="tab" style="cursor:pointer" id="fin">Financials</div>
  <button id="buy">Buy</button> <button id="sell">بيع</button>
  <div style="cursor:pointer" id="buy-div"><span>شراء</span></div></nav>
<input type="search" placeholder="Search stocks" name="q">
<form id="ticket"><input type="number" name="qty"><button type="button" id="max">Max</button>
  <button type="submit" id="go">Go</button></form>
<script>
  window.log = [];
  document.addEventListener('click', (e) => {
    e.preventDefault();
    window.log.push(e.target.id || e.target.parentElement.id);
  }, true);
  document.querySelector('input[name=q]').addEventListener('input',
    (e) => window.log.push('typed:' + e.target.value));
</script></body></html>"""

CHROMIUM = os.environ.get("EGX_E2E_CHROMIUM", "")


@pytest.fixture
def tab():
    if not CHROMIUM:
        pytest.skip("set EGX_E2E_CHROMIUM to a Chromium executable to run browser tests")
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        chromium = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        page = chromium.new_page()
        page.route("https://x.thndr.app/", lambda route: route.fulfill(
            body=FAKE_THNDR, content_type="text/html; charset=utf-8"))
        page.goto("https://x.thndr.app/")
        yield page
        chromium.close()


def js(tab, script):
    return json.loads(tab.evaluate(script))


def test_real_page_read_lists_tabs_but_no_order_buttons(tab):
    out = js(tab, browse.read_script())
    assert "News" in out["labels"] and "Financials" in out["labels"]
    for label in ("Buy", "بيع", "شراء", "Max", "Go", "Offers"):
        assert label not in out["labels"]


def test_real_page_clicks_a_tab(tab):
    assert js(tab, browse.click_script("financials", "x.thndr.app")) == {
        "ok": True, "clicked": "Financials"}
    assert tab.evaluate("window.log") == ["fin"]


@pytest.mark.parametrize(("text", "reason"), [
    ("Max", "in_ticket"), ("Go", "submits"), ("Offers", "off_site"),
])
def test_real_page_refuses_unsafe_elements(tab, text, reason):
    assert js(tab, browse.click_script(text, "x.thndr.app"))["reason"] == reason
    assert tab.evaluate("window.log") == []


def test_real_page_refuses_buy_even_when_asked_indirectly(tab):
    # "uy" matches the Buy button's text; the element's own words refuse it.
    assert js(tab, browse.click_script("uy", "x.thndr.app"))["reason"] == "forbidden"
    assert js(tab, browse.click_script("شرا", "x.thndr.app"))["reason"] == "forbidden"
    assert tab.evaluate("window.log") == []


def test_real_page_search_types_into_the_search_box(tab):
    assert js(tab, browse.search_script("COMI")) == {"ok": True}
    assert tab.evaluate("window.log") == ["typed:COMI"]


def test_real_page_pointing_at_buy_clicks_nothing(tab):
    assert js(tab, browse.POINT_AT_BUY_SCRIPT)["marked"] >= 1
    assert tab.evaluate("window.log") == []


def test_a_move_waits_until_the_page_stops_changing_not_a_fixed_time():
    page = FakePage()
    drawn = iter(["1:a", "5:ab", "9:abc", "9:abc", "9:abc", "9:abc"])
    page.run = lambda script: next(drawn) if "slice(-300)" in script else "{}"
    waits: list[float] = []
    b = browse.Browser(page, HOME, settle=3.0, sleep=waits.append)
    b._wait_drawn()
    assert waits == [0.25] * 5, "done once the text held still twice in a row"
    # A page that never settles is left after `settle` seconds, not waited on forever.
    page.run = lambda script: str(time.monotonic())
    b2 = browse.Browser(page, HOME, settle=0.3, sleep=lambda s: time.sleep(0.05))
    started = time.monotonic()
    b2._wait_drawn()
    assert time.monotonic() - started < 1.0

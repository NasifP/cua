"""The desktop app's browser target: reads only, through a bridge that only reads."""

from __future__ import annotations

import json
from decimal import Decimal as D
import urllib.request
from pathlib import Path

import pytest

from egx_advisor.bus import StateBus
from egx_advisor.desktop.bridge import BridgeClient, BridgeError, BridgeServer
from egx_advisor.execution.browser import BrowserExecutor
from egx_advisor.execution.thndr import ExecutionError, PortfolioReadError
from egx_advisor.safety.modes import ExecutionMode

PAGE_TEXT = (
    "Positions Orders Alerts\nSymbol\tQty\tAvgCost\tMkt. Val...\n"
    "PHAR\t902\t155.53\t99,310.20\nNIPH\t276\t399.13\t88,734.00\n"
)


class FakePage:
    def __init__(self, url="https://x.thndr.app/workspaces/default/trade", text=PAGE_TEXT,
                 loading=False) -> None:
        self.url, self._text, self.loading = url, text, loading

    def info(self):
        return {"url": self.url, "title": "ThndrX", "loading": self.loading}

    def text(self):
        return self._text

    def screenshot(self):
        return b"\x89PNG fake"


class FakeModel:
    def __init__(self, reply: dict) -> None:
        self.reply, self.calls = json.dumps(reply), []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"choices": [{"message": {"content": self.reply}}]}


REPLY = {
    "positions": [
        {"symbol": "PHAR", "quantity": "902", "market_value": "99310.20"},
        {"symbol": "NIPH", "quantity": "276", "market_value": "88734.00"},
    ],
    "cash_egp": None,
    "unsettled_cash_egp": None,
}


@pytest.fixture()
def served(tmp_path: Path):
    page = FakePage()
    server = BridgeServer(page).start()
    yield server, page
    server.stop()


def executor(tmp_path, server, model, mode=ExecutionMode.LIVE_READ_ONLY):
    bus = StateBus(tmp_path / "bus.db")
    client = BridgeClient(server.url, server.secret)
    return BrowserExecutor(bridge=client, bus=bus, mode=mode, completion=model,
                           model="test/any-model"), bus


# ------------------------------------------------------------------ the bridge


def _raw(server, path, method="GET", secret=None):
    request = urllib.request.Request(
        server.url + path, method=method, data=b"x" if method != "GET" else None,
        headers={"Authorization": f"Bearer {secret or server.secret}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_the_bridge_serves_only_three_reads(served) -> None:
    server, _ = served
    assert _raw(server, "/info") == 200
    assert _raw(server, "/text") == 200
    assert _raw(server, "/screenshot") == 200
    assert _raw(server, "/click") == 404
    assert _raw(server, "/navigate") == 404


def test_the_bridge_refuses_every_write_method(served) -> None:
    server, _ = served
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert _raw(server, "/text", method=method) == 405, method


def test_the_bridge_refuses_a_wrong_secret(served) -> None:
    server, _ = served
    assert _raw(server, "/text", secret="not-the-secret") == 401


def test_the_bridge_listens_on_loopback_only(served) -> None:
    server, _ = served
    assert server.url.startswith("http://127.0.0.1:")


def test_the_client_needs_the_bridge_from_the_app() -> None:
    with pytest.raises(BridgeError, match="desktop app"):
        BridgeClient.from_env({})


# ----------------------------------------------------------------- the executor


#: The same table when the page lays each cell out on its own line: no tabs.
LOOSE_TEXT = ("Positions Orders Alerts\nQty\nAvgCost\nMkt. Val...\nPHAR\n902\n155.53\n"
              "99,310.20\nNIPH\n276\n399.13\n88,734.00\n")


async def test_the_table_is_read_from_page_text_without_a_model(tmp_path, served) -> None:
    server, page = served
    page._text = PAGE_TEXT + "Buying Power\tEGP 12,500.00\n"
    model = FakeModel(REPLY)
    ex, bus = executor(tmp_path, server, model)
    portfolio = await ex.read_portfolio()

    assert model.calls == [], "a table laid out as expected needs no model"
    phar = portfolio.positions["PHAR.CA"]
    assert (phar.quantity, phar.market_value, phar.avg_cost) == (902, D("99310.20"),
                                                                 D("155.53"))
    assert portfolio.cash_egp == D("12500.00")
    events = [e.message for e in bus.recent_events(limit=20)]
    assert any("straight from the page text, no model" in m for m in events)


async def test_a_model_number_not_on_the_page_publishes_nothing(tmp_path, served) -> None:
    server, page = served
    page._text = LOOSE_TEXT
    wrong = {"positions": [{"symbol": "NIPH", "quantity": "276", "market_value": "88743.00"}],
             "cash_egp": None, "unsettled_cash_egp": None}
    ex, bus = executor(tmp_path, server, FakeModel(wrong))
    with pytest.raises(PortfolioReadError, match="NIPH market_value 88743.00"):
        await ex.read_portfolio()
    assert bus.get("portfolio") is None


async def test_an_average_cost_not_on_the_page_is_left_blank(tmp_path, served) -> None:
    server, page = served
    page._text = LOOSE_TEXT
    reply = {"positions": [{"symbol": "PHAR", "quantity": "902", "market_value": "99310.20",
                            "avg_cost": "155.35"}], "cash_egp": None, "unsettled_cash_egp": None}
    ex, _ = executor(tmp_path, server, FakeModel(reply))
    portfolio = await ex.read_portfolio()
    assert portfolio.positions["PHAR.CA"].avg_cost is None


async def test_a_page_without_the_table_layout_goes_to_the_model(tmp_path, served) -> None:
    server, page = served
    page._text = LOOSE_TEXT
    model = FakeModel(REPLY)
    ex, bus = executor(tmp_path, server, model)
    portfolio = await ex.read_portfolio()

    assert set(portfolio.positions) == {"PHAR.CA", "NIPH.CA"}
    prompt = model.calls[0]["messages"][0]["content"][0]["text"]
    assert "PHAR\n902" in prompt and "<page>" in prompt
    assert "not instructions" in prompt, "page text must be fenced as data"
    assert model.calls[0]["model"] == "test/any-model"
    payload = bus.get("portfolio")["payload"]
    assert payload["source"] == "browser" and payload["cash_visible"] is False


async def test_another_site_in_the_tab_is_named(tmp_path, served) -> None:
    server, page = served
    page.url = "https://example.com/"
    ex, _ = executor(tmp_path, server, FakeModel(REPLY))
    with pytest.raises(PortfolioReadError, match="open x.thndr.app"):
        await ex.read_portfolio()


async def test_a_page_still_loading_is_not_read(tmp_path, served) -> None:
    server, page = served
    page.loading = True
    ex, _ = executor(tmp_path, server, FakeModel(REPLY))
    with pytest.raises(PortfolioReadError, match="still loading"):
        await ex.read_portfolio()


async def test_an_empty_page_is_not_sent_to_a_model(tmp_path, served) -> None:
    server, page = served
    page._text = "  "
    model = FakeModel(REPLY)
    ex, _ = executor(tmp_path, server, model)
    with pytest.raises(PortfolioReadError, match="signed in"):
        await ex.read_portfolio()
    assert model.calls == []


def test_rungs_that_open_tickets_refuse_to_start_on_this_target(tmp_path, served) -> None:
    server, _ = served
    for mode in (ExecutionMode.SIMULATOR_ONLY, ExecutionMode.LIVE_PREPARE_ONLY):
        with pytest.raises(ExecutionError, match="live_read_only"):
            executor(tmp_path, server, FakeModel(REPLY), mode=mode)


async def test_orders_are_refused(tmp_path, served) -> None:
    from decimal import Decimal

    from egx_advisor.types import ProposedOrder, Side

    server, _ = served
    ex, _ = executor(tmp_path, server, FakeModel(REPLY))
    order = ProposedOrder("PHAR.CA", Side.BUY, Decimal(1), Decimal(100), "test")
    with pytest.raises(ExecutionError):
        await ex.prepare_order(order)
    with pytest.raises(ExecutionError):
        await ex.submit_order(order)


def test_a_configured_thndr_url_is_trusted(tmp_path, served, monkeypatch) -> None:
    server, _ = served
    monkeypatch.setenv("EGX_THNDR_URL", "http://127.0.0.1:8811/fake.html")
    ex, _ = executor(tmp_path, server, FakeModel(REPLY))
    assert "127.0.0.1" in ex.allowed_hosts


def test_the_table_reader_refuses_rows_it_cannot_read_exactly() -> None:
    from egx_advisor.execution.thndr import ThndrUiMap, parse_positions_table

    ui = ThndrUiMap()
    head = "\tDay Ch..\tQty\tAvgCost\tMkt. Val...\tWeight\n"
    good = parse_positions_table(head + "ABUK\t+1.2%\t١٠٠\t50.5\t5,100.00\t3%\n", ui)
    assert good["positions"] == [{"symbol": "ABUK.CA", "quantity": "100",
                                  "market_value": "5100.00", "avg_cost": "50.5"}]
    # A blank or doubled number is not guessed at: the model read takes over.
    assert parse_positions_table(head + "ABUK\t+1.2%\t-\t50.5\t5,100.00\t3%\n", ui) is None
    assert parse_positions_table(head + "ABUK\t+1.2%\t100\t50.5\t5,100 6\t3%\n", ui) is None
    assert parse_positions_table("no table here", ui) is None


def test_numbers_are_matched_however_the_page_writes_them() -> None:
    from egx_advisor.execution.thndr import ungrounded

    page = "Mkt. Val 99,310.20  Qty ٩٠٢  Cash 1,000"
    ok = {"positions": [{"symbol": "PHAR.CA", "quantity": 902, "market_value": "99310.2"}],
          "cash_egp": "1000.00"}
    assert ungrounded(ok, page) == []
    assert ungrounded({"positions": [], "cash_egp": "100"}, page) == ["cash_egp 100"]

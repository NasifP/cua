"""The desktop app's browser target: reads only, through a bridge that only reads."""

from __future__ import annotations

import json
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


async def test_the_portfolio_is_read_from_page_text(tmp_path, served) -> None:
    server, _ = served
    model = FakeModel(REPLY)
    ex, bus = executor(tmp_path, server, model)
    portfolio = await ex.read_portfolio()

    assert set(portfolio.positions) == {"PHAR.CA", "NIPH.CA"}
    prompt = model.calls[0]["messages"][0]["content"][0]["text"]
    assert "PHAR\t902" in prompt and "<page>" in prompt
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

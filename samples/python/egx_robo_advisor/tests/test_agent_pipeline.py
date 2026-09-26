"""End-to-end ordering properties of the agent loop.

The claim under test is the one the whole architecture rests on: the news layer
decides what is permitted **before** the bot is allowed to touch the screen, and
it can only ever narrow what follows.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from egx_advisor.bus import StateBus
from egx_advisor.egx_cua_agent import AgentConfig, EgxCuaAgent
from egx_advisor.execution.thndr import GuardedComputer, ThndrUiMap
from egx_advisor.regime.filter import RegimeFilter
from egx_advisor.regime.sentiment import KeywordClassifier
from egx_advisor.regime.sources import FeedPollResult
from egx_advisor.safety.demo_guard import DemoGuard
from egx_advisor.safety.guarded_interface import GuardedInterface
from egx_advisor.safety.pngutil import encode_png
from egx_advisor.types import (
    AgentPhase,
    Headline,
    MarketSnapshot,
    Quote,
    Tradability,
)

BLANK = encode_png(8, 8, bytes(8 * 8 * 3))
# A Tuesday, inside the EGX continuous session (11:00 Cairo == 09:00 UTC).
SESSION_TIME = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)

PORTFOLIO_JSON = json.dumps(
    {
        "cash_egp": "1000000",
        "unsettled_cash_egp": "0",
        "positions": [{"symbol": "COMI.CA", "quantity": "100", "market_value": "5000"}],
    }
)


class FakeInterface:
    def __init__(self) -> None:
        self.labels = ["Simulator"]
        self.screenshots = 0
        self.clicks: list[tuple] = []

    @property
    def tree(self) -> dict:
        return {"children": [{"label": label} for label in self.labels]}

    async def screenshot(self) -> bytes:
        self.screenshots += 1
        return BLANK

    async def left_click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    async def type_text(self, text: str) -> None:
        self.clicks.append(("type", text))


class FakeComputer:
    """Counts every attempt to reach the screen."""

    def __init__(self) -> None:
        self._iface = FakeInterface()
        self._initialized = True
        self.interface_accesses = 0

    @property
    def interface(self) -> FakeInterface:
        self.interface_accesses += 1
        return self._iface

    async def run(self) -> None:
        return None


class FakeAgent:
    """Stands in for cua_agent.ComputerAgent."""

    def __init__(self, computer) -> None:
        self.computer = computer
        self.instructions: list[str] = []

    async def run(self, history, stream: bool = False):
        instruction = history[-1]["content"]
        self.instructions.append(instruction)
        if "return ONLY a JSON object" in instruction:
            reply = PORTFOLIO_JSON
        elif "exactly one word" in instruction:
            reply = "PLACED"
        else:
            reply = "done"
        yield {"output": [{"type": "message", "content": [{"text": reply}]}]}


class FakeVision:
    """Stands in for litellm.completion on the portfolio read."""

    def __init__(self, reply: str = "") -> None:
        self.reply = reply or PORTFOLIO_JSON
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"choices": [{"message": {"content": self.reply}}]}


class FakeNews:
    def __init__(self, titles=()) -> None:
        self.titles = list(titles)
        self.polls = 0

    async def poll(self, *, now=None):
        self.polls += 1
        moment = now or datetime.now(timezone.utc)
        return FeedPollResult(
            headlines=tuple(
                Headline("test", title, f"https://example.test/{i}", moment)
                for i, title in enumerate(self.titles)
            ),
            polled_at=moment,
        )


class FakeMarketData:
    def __init__(self) -> None:
        self.calls = 0

    async def snapshot(self, universe) -> MarketSnapshot:
        self.calls += 1
        return MarketSnapshot(
            as_of=SESSION_TIME,
            quotes={
                i.symbol: Quote(i.symbol, D("50"), D("50"), SESSION_TIME, Tradability.OPEN)
                for i in universe
            },
            usd_egp=D("50"),
            usd_egp_lookback=D("48"),
        )


def build(tmp_path: Path, *, titles=(), execute=True, calibrated=True):
    bus = StateBus(tmp_path / "state.db")
    bus.resume(actor="test", reason="armed")
    computer = FakeComputer()
    news = FakeNews(titles)
    market = FakeMarketData()
    agents: list[FakeAgent] = []

    def factory(guarded_computer):
        agent = FakeAgent(guarded_computer)
        agents.append(agent)
        return agent

    agent = EgxCuaAgent(
        config=AgentConfig(
            bus_path=str(tmp_path / "state.db"),
            execute_orders=execute,
            ui=ThndrUiMap(calibration_complete=calibrated),
            arm_delay=0,
        ),
        computer=computer,
        market_data=market,
        agent_factory=factory,
        news_fetcher=news,
        vision_completion=FakeVision(),
        classifiers=(KeywordClassifier(),),
        regime_filter=RegimeFilter(),
        demo_guard=DemoGuard(text_probes=(), colour_probe=None),
        bus=bus,
    )
    # The guard reads the accessibility tree from the fake interface.
    agent.guard = DemoGuard(text_probes=(), colour_probe=None)
    return agent, bus, computer, news, market, agents


@pytest.fixture(autouse=True)
def _freeze_session(monkeypatch):
    """Pin 'now' inside the trading session so the calendar gate passes."""
    import egx_advisor.egx_cua_agent as module

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return SESSION_TIME if tz else SESSION_TIME.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", FrozenDatetime)


def _wire_tree(agent, computer):
    """Attach the accessibility provider the real wiring supplies."""
    original = agent._ensure_connected

    async def ensure():
        executor = await original()
        agent._interface._tree_provider = lambda: computer._iface.tree
        return executor

    agent._ensure_connected = ensure


# --------------------------------------------------------------------------- #


async def test_all_halted_regime_never_touches_the_screen(tmp_path: Path) -> None:
    """The central ordering claim.

    On ALL_HALTED the cycle returns before `_ensure_connected()`, so no interface
    is ever constructed and nothing exists in scope that could click.
    """
    agent, bus, computer, news, market, agents = build(tmp_path)
    agent.regime_filter.panic("simulated data integrity failure")

    phase = await agent._run_cycle()

    assert phase is AgentPhase.HALTED
    assert news.polls == 1, "news must still be polled"
    assert computer.interface_accesses == 0, "no interface may be constructed"
    assert computer._iface.screenshots == 0, "no screenshot may be taken"
    assert computer._iface.clicks == []
    assert market.calls == 0, "we never even priced the universe"


async def test_kill_switch_stops_the_cycle_before_the_screen(tmp_path: Path) -> None:
    agent, bus, computer, news, market, agents = build(tmp_path)
    bus.halt(actor="mobile", reason="KILL SWITCH")

    phase = await agent._run_cycle()

    assert phase is AgentPhase.HALTED
    assert news.polls == 0, "a halted bot does not even poll"
    assert computer.interface_accesses == 0


async def test_catastrophic_news_suppresses_buys_but_the_plan_still_forms(
    tmp_path: Path,
) -> None:
    """News subtracts from a plan that was built without consulting it."""
    agent, bus, computer, news, market, agents = build(
        tmp_path, titles=("Central bank devalues the Egyptian pound by 20%",)
    )
    _wire_tree(agent, computer)

    await agent._run_cycle()

    plan = bus.get("plan")
    assert plan is not None
    payload = plan["payload"]
    assert payload["suppressed"], "buys should have been withheld"
    assert all(entry["side"] == "buy" for entry in payload["suppressed"])
    assert all("buys halted" in entry["reason"] for entry in payload["suppressed"])


async def test_clean_news_allows_the_plan_through(tmp_path: Path) -> None:
    agent, bus, computer, news, market, agents = build(tmp_path)
    _wire_tree(agent, computer)

    phase = await agent._run_cycle()

    regime = bus.get("regime")
    assert regime is not None and regime["payload"]["risk_state"] == "risk_on"
    assert phase in (AgentPhase.EXECUTING, AgentPhase.PLANNING)
    assert computer._iface.screenshots > 0, "the screen was read on the happy path"


async def test_agent_factory_receives_the_guarded_computer(tmp_path: Path) -> None:
    """The integration point that stops an LLM-driven agent bypassing the guard."""
    agent, bus, computer, news, market, agents = build(tmp_path)
    _wire_tree(agent, computer)

    await agent._run_cycle()

    assert agents, "the agent factory should have been called"
    handed = agents[0].computer
    assert isinstance(handed, GuardedComputer)
    assert isinstance(handed.interface, GuardedInterface)
    assert handed.interface is not computer._iface


async def test_dry_run_plans_but_never_submits(tmp_path: Path) -> None:
    agent, bus, computer, news, market, agents = build(tmp_path, execute=False)
    _wire_tree(agent, computer)

    phase = await agent._run_cycle()

    assert phase is AgentPhase.PLANNING
    orders = [e for e in bus.recent_events(limit=100) if e.kind == "order"]
    assert not orders, "dry run must not submit anything"


async def test_out_of_session_idles_without_touching_the_screen(
    tmp_path: Path, monkeypatch
) -> None:
    import egx_advisor.egx_cua_agent as module

    friday = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)

    class FridayDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return friday if tz else friday.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", FridayDatetime)
    agent, bus, computer, news, market, agents = build(tmp_path)

    phase = await agent._run_cycle()

    assert phase is AgentPhase.IDLE
    assert computer.interface_accesses == 0
    status = bus.get("status")
    assert status is not None and status["payload"]["session"] == "closed_weekend"


async def test_uncalibrated_ui_refuses_to_submit_orders(tmp_path: Path) -> None:
    """An uncalibrated UI map must not be allowed to click Buy."""
    from egx_advisor.execution.thndr import ExecutionError, ThndrExecutor
    from egx_advisor.types import ProposedOrder, Side

    bus = StateBus(tmp_path / "state.db")
    bus.resume(actor="test", reason="armed")
    raw = FakeInterface()
    guarded = GuardedInterface(
        raw,
        bus=bus,
        guard=DemoGuard(text_probes=(), colour_probe=None),
        accessibility_tree_provider=lambda: raw.tree,
    )
    executor = ThndrExecutor(
        interface=guarded, bus=bus, ui=ThndrUiMap(calibration_complete=False), agent=object()
    )
    order = ProposedOrder("COMI.CA", Side.BUY, D(100), D(85), "drift")

    with pytest.raises(ExecutionError, match="calibration_complete"):
        await executor.submit_order(order)
    assert raw.clicks == []


# --------------------------------------------------------------- UI calibration


def test_ui_map_loads_from_toml(tmp_path: Path) -> None:
    """Calibration is data a human edits after looking at their screen."""
    path = tmp_path / "ui.toml"
    path.write_text(
        '[ui]\nbuy_button_label = "Buy shares"\n'
        'simulator_option_label = "Virtual Portfolio"\n'
        "calibration_complete = true\n",
        encoding="utf-8",
    )
    ui = ThndrUiMap.from_toml(path)
    assert ui.buy_button_label == "Buy shares"
    assert ui.simulator_option_label == "Virtual Portfolio"
    assert ui.calibration_complete is True
    # Unspecified keys keep their defaults rather than becoming empty.
    assert ui.confirm_button_label == "Confirm"


def test_ui_map_rejects_an_unknown_key(tmp_path: Path) -> None:
    """A typo must fail loudly, not silently leave a placeholder in place."""
    path = tmp_path / "typo.toml"
    path.write_text('[ui]\nbuy_buton_label = "Buy"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown key"):
        ThndrUiMap.from_toml(path)


def test_shipped_ui_config_is_not_marked_calibrated() -> None:
    """The file in the repo must never claim someone checked their own screen."""
    shipped = Path(__file__).resolve().parent.parent / "config" / "thndr.ui.toml"
    assert shipped.exists()
    assert ThndrUiMap.from_toml(shipped).calibration_complete is False


def test_broker_symbols_drop_the_data_suffix() -> None:
    """Thndr X lists bare EGX tickers; typing 'COMI.CA' into its search finds nothing."""
    ui = ThndrUiMap()
    assert ui.broker_symbol("COMI.CA") == "COMI"
    assert ui.broker_symbol("ABUK.CA") == "ABUK"
    # Already-bare symbols pass through untouched.
    assert ui.broker_symbol("NIPH") == "NIPH"


def test_symbols_round_trip_back_to_canonical() -> None:
    """A portfolio read off the screen must be re-tagged before it sizes anything."""
    ui = ThndrUiMap()
    for canonical in ("COMI.CA", "TMGH.CA", "AZG.CA"):
        assert ui.canonical_symbol(ui.broker_symbol(canonical)) == canonical
    assert ui.canonical_symbol("niph") == "NIPH.CA"


def test_symbol_overrides_win_in_both_directions() -> None:
    ui = ThndrUiMap(symbol_overrides={"AZG.CA": "AZGD"})
    assert ui.broker_symbol("AZG.CA") == "AZGD"
    assert ui.canonical_symbol("AZGD") == "AZG.CA"


async def test_portfolio_read_retags_broker_tickers(tmp_path: Path) -> None:
    """End to end: the agent's JSON uses broker tickers, the Portfolio uses canonical."""
    from egx_advisor.execution.thndr import _parse_portfolio

    ui = ThndrUiMap()
    payload = {
        "cash_egp": "100000",
        "positions": [{"symbol": "PHAR", "quantity": "902", "market_value": "104181"}],
    }
    for entry in payload["positions"]:
        entry["symbol"] = ui.canonical_symbol(entry["symbol"])
    portfolio = _parse_portfolio(payload, demo_confirmed=True)
    assert "PHAR.CA" in portfolio.positions



# ---------------------------------------------------------------- lifecycle


def test_every_start_is_a_halted_start(tmp_path: Path) -> None:
    """The control row outlives the process, and Ctrl-C on Windows does not halt.

    An agent stopped while armed used to come back armed on the next start.
    """
    agent, bus, *_ = build(tmp_path)
    assert not bus.is_halted(), "precondition: left armed by a previous run"
    agent._announce_startup()
    assert bus.is_halted()


async def test_an_unreadable_input_is_retried_not_latched(tmp_path: Path) -> None:
    """One bad read used to latch ALL_HALTED for 24 hours, with nothing to clear it."""
    from egx_advisor.execution.thndr import PortfolioReadError

    agent, bus, *_ = build(tmp_path)

    async def failing_cycle():
        agent.request_stop()
        raise PortfolioReadError("reply did not parse")

    agent._run_cycle = failing_cycle
    await agent.run_forever()

    assert not agent.regime_filter.panicking
    messages = [e.message for e in bus.recent_events(limit=20)]
    assert any("will retry next cycle" in m for m in messages)


async def _run_until(agent, predicate, timeout: float = 5.0) -> None:
    import asyncio

    task = asyncio.create_task(agent.run_forever())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while loop.time() < deadline and not predicate():
            await asyncio.sleep(0.05)
    finally:
        agent.request_stop()
        await asyncio.wait_for(task, timeout=5)


async def test_an_arm_is_answered_within_seconds(tmp_path: Path) -> None:
    """While halted the loop used to sleep a full idle interval (120 s) between
    checks, so pressing START on the dashboard did nothing for up to two minutes."""
    import asyncio

    agent, bus, computer, *_ = build(tmp_path)
    agent.config.idle_interval = 120.0
    agent.config.arm_delay = 0.2

    async def arm_soon():
        await asyncio.sleep(0.3)
        bus.resume(actor="test", reason="armed")

    asyncio.get_running_loop().create_task(arm_soon())
    await _run_until(agent, lambda: computer.interface_accesses > 0, timeout=4.0)
    assert computer.interface_accesses > 0, "the arm was not answered within seconds"


async def test_halting_during_the_countdown_touches_nothing(tmp_path: Path) -> None:
    """Pressing START leaves the dashboard in front. The countdown is the operator's
    time to bring the broker forward, and halting inside it must cancel the read."""
    import asyncio

    agent, bus, computer, *_ = build(tmp_path)
    agent.config.arm_delay = 3.0
    agent.config.halted_poll_interval = 0.05

    async def arm_then_halt():
        await asyncio.sleep(0.2)
        bus.resume(actor="test", reason="armed")
        await asyncio.sleep(0.8)
        bus.halt(actor="test", reason="changed my mind")

    asyncio.get_running_loop().create_task(arm_then_halt())

    def cancelled() -> bool:
        return any("countdown cancelled" in e.message for e in bus.recent_events(limit=20))

    await _run_until(agent, cancelled, timeout=4.0)
    assert cancelled()
    assert computer.interface_accesses == 0
    messages = [e.message for e in bus.recent_events(limit=30)]
    assert any("first screenshot in 3 s" in m for m in messages)


async def test_a_guard_refusal_with_the_bus_armed_does_not_spin(tmp_path: Path) -> None:
    """A cycle can end HALTED while the bus stays armed. Waking early there, as an
    arm does, would re-run the cycle at once against the screen and the model."""
    import asyncio

    agent, bus, computer, *_ = build(tmp_path)
    agent.config.arm_delay = 0
    agent.config.idle_interval = 30.0
    cycles = 0

    async def refused_cycle():
        nonlocal cycles
        cycles += 1
        return AgentPhase.HALTED

    agent._run_cycle = refused_cycle
    agent._announce_startup = lambda: None
    task = asyncio.create_task(agent.run_forever())
    await asyncio.sleep(0.5)
    agent.request_stop()
    await asyncio.wait_for(task, timeout=5)
    assert not bus.is_halted()
    assert cycles == 1, f"re-ran {cycles} times in half a second"


async def test_the_plan_orders_are_kept_for_the_pick_review(tmp_path: Path) -> None:
    from egx_advisor.memory import Memory

    agent, bus, computer, news, market, agents = build(tmp_path, execute=False)
    _wire_tree(agent, computer)
    agent.memory = Memory(tmp_path / "memory.db")

    await agent._run_cycle()

    orders = bus.get("plan")["payload"]["orders"]
    picks = agent.memory.picks("plan")
    assert orders, "the fixture plan has orders to keep"
    assert {(p.symbol, p.side) for p in picks} == {(o["symbol"], o["side"]) for o in orders}
    assert all(p.price > 0 for p in picks)

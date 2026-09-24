"""Dashboard endpoints, with the kill switch as the critical path.

Skipped when FastAPI is not installed: the dashboard is an optional extra, and
the safety core must stay testable without it.
"""

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from dashboard.app import DashboardConfig, create_app  # noqa: E402
from egx_advisor.bus import EventKind  # noqa: E402

TOKEN = "test-token-that-is-long-enough-1234"


@pytest.fixture
def client(tmp_path: Path):
    config = DashboardConfig(bus_path=str(tmp_path / "state.db"), token=TOKEN)
    app = create_app(config)
    # Arming echoes the mode the agent reported, so a bus with no agent in it
    # cannot arm at all. Most tests here are about something else, so give them
    # an agent that has checked in.
    app.state.bus.put("mode", {"mode": "simulator_only", "banner": "SIMULATOR ONLY"})
    return TestClient(app), app.state.bus


def test_requires_authentication(client) -> None:
    api, _ = client
    assert api.get("/api/state").status_code == 401
    assert api.post("/api/control/halt").status_code == 401


def test_healthz_is_open_but_says_nothing_sensitive(client) -> None:
    api, _ = client
    response = api.get("/healthz")
    assert response.status_code == 200
    assert set(response.json()) == {"ok", "ts"}


def test_short_token_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="at least 24 characters"):
        DashboardConfig(bus_path=str(tmp_path / "s.db"), token="short")


def test_missing_token_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="will not start without a token"):
        DashboardConfig(bus_path=str(tmp_path / "s.db"), token="")


def test_importing_the_module_has_no_side_effects() -> None:
    """No database opened, no environment read, no exception on import."""
    import importlib

    import dashboard.app as app_module

    importlib.reload(app_module)  # must not raise even with no env configured


def test_session_cookie_is_not_the_token(tmp_path: Path) -> None:
    config = DashboardConfig(bus_path=str(tmp_path / "s.db"), token=TOKEN)
    assert config.session_value != TOKEN
    assert len(config.session_value) == 64


def test_bearer_token_grants_access(client) -> None:
    api, _ = client
    response = api.get("/api/state", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert "control" in response.json()


def test_index_swaps_a_url_token_for_a_cookie_and_leaves_the_url(client) -> None:
    """So the token stops appearing in the address bar and the phone's screen."""
    api, _ = client
    response = api.get(f"/?token={TOKEN}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.cookies.get("egx_session")
    assert cookie and cookie != TOKEN, "the cookie must not be the token itself"


def test_the_launcher_link_signs_in_without_the_password_in_it(client) -> None:
    """start.cmd used to open /?token=<password>, leaving it in browser history."""
    from egx_advisor.login_link import make_login_path

    api, _ = client
    link = make_login_path(TOKEN)
    assert TOKEN not in link
    response = api.get(link, follow_redirects=False)
    assert response.status_code == 303
    assert response.cookies.get("egx_session")


def test_a_login_link_works_once(client) -> None:
    from egx_advisor.login_link import make_login_path

    api, _ = client
    link = make_login_path(TOKEN)
    assert api.get(link, follow_redirects=False).status_code == 303
    api.cookies.clear()
    assert api.get(link, follow_redirects=False).status_code == 401


def test_an_expired_or_forged_login_link_is_refused(client) -> None:
    import time

    from egx_advisor.login_link import make_login_path

    api, _ = client
    old = make_login_path(TOKEN, now=time.time() - 600)
    assert api.get(old, follow_redirects=False).status_code == 401
    forged = make_login_path("someone-elses-password-long-enough")
    assert api.get(forged, follow_redirects=False).status_code == 401
    assert api.get("/login?ts=abc&sig=", follow_redirects=False).status_code == 401


def test_kill_switch_halts_the_agent(client) -> None:
    api, bus = client
    bus.resume(actor="test", reason="armed")
    assert not bus.is_halted()

    response = api.post("/api/control/halt", headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200
    assert response.json()["control"]["halted"] is True
    assert bus.is_halted(), "the agent's own view of the bus must show halted"


def test_kill_switch_is_idempotent(client) -> None:
    api, bus = client
    headers = {"Authorization": f"Bearer {TOKEN}"}
    api.post("/api/control/halt", headers=headers)
    assert api.post("/api/control/halt", headers=headers).status_code == 200
    assert bus.is_halted()


def test_arming_requires_the_page_to_name_the_current_mode(client) -> None:
    """A bare POST is a mis-tap or a replay. Neither should arm an order-placer."""
    api, bus = client
    headers = {"Authorization": f"Bearer {TOKEN}"}
    api.post("/api/control/halt", headers=headers)

    for wrong in ("", "simulator", "SIMULATOR_ONLY", "live_read_only"):
        response = api.post(
            "/api/control/resume", json={"confirm": wrong}, headers=headers
        )
        assert response.status_code == 409
        assert bus.is_halted(), f"{wrong!r} must not arm the bot"


def test_arming_works_when_the_page_echoes_the_live_mode(client) -> None:
    api, bus = client
    headers = {"Authorization": f"Bearer {TOKEN}"}
    api.post("/api/control/halt", headers=headers)

    response = api.post(
        "/api/control/resume", json={"confirm": "simulator_only"}, headers=headers
    )

    assert response.status_code == 200
    assert not bus.is_halted()


def test_arming_is_refused_when_no_agent_has_reported_a_mode(tmp_path: Path) -> None:
    """Fail closed. Arming a bot we cannot describe is the case to refuse."""
    config = DashboardConfig(bus_path=str(tmp_path / "state.db"), token=TOKEN)
    app = create_app(config)
    api = TestClient(app)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    response = api.post(
        "/api/control/resume", json={"confirm": "simulator_only"}, headers=headers
    )

    assert response.status_code == 409
    assert app.state.bus.is_halted()


def test_a_page_left_open_through_a_mode_change_cannot_arm(client) -> None:
    """The reason arming echoes the mode at all.

    A dashboard open since this morning is showing whatever the agent was doing
    then. If the account behind it changed, the operator pressing confirm is
    agreeing to the old banner. The echo turns that into a refusal instead of a
    silent arm on stale information.
    """
    api, bus = client
    headers = {"Authorization": f"Bearer {TOKEN}"}
    api.post("/api/control/halt", headers=headers)
    bus.put("mode", {"mode": "live_read_only", "banner": "REAL ACCOUNT - READ ONLY"})

    stale = api.post(
        "/api/control/resume", json={"confirm": "simulator_only"}, headers=headers
    )

    assert stale.status_code == 409
    assert "reload" in stale.json()["detail"]
    assert bus.is_halted(), "a stale page must not arm the bot"

    # Reloading shows the new mode, and arming then works.
    fresh = api.post(
        "/api/control/resume", json={"confirm": "live_read_only"}, headers=headers
    )
    assert fresh.status_code == 200
    assert not bus.is_halted()


def test_state_surfaces_what_the_agent_published(client) -> None:
    """The agent -> dashboard contract, end to end."""
    api, bus = client
    bus.put("regime", {"risk_state": "buys_halted", "drivers": ["devaluation"]})
    bus.put("portfolio", {"total_value": "1000000", "positions": []})
    bus.publish(EventKind.UI_ACTION, "left_click(412, 880)", phase="executing")

    payload = api.get("/api/state", headers={"Authorization": f"Bearer {TOKEN}"}).json()

    assert payload["regime"]["risk_state"] == "buys_halted"
    assert payload["portfolio"]["total_value"] == "1000000"
    assert any("left_click(412, 880)" in e["message"] for e in payload["events"])


def test_control_audit_is_exposed(client) -> None:
    api, bus = client
    headers = {"Authorization": f"Bearer {TOKEN}"}
    api.post("/api/control/halt", headers=headers)
    rows = api.get("/api/control/audit", headers=headers).json()["audit"]
    assert any(row["halted"] for row in rows)


# ------------------------------------------------------------------- chat


class FakeAssistant:
    model = "gemini/test"

    def __init__(self):
        self.asked: list[str] = []

    def answer(self, question, history=()):
        self.asked.append(question)
        return f"answered: {question}"


@pytest.fixture
def chat_client(tmp_path: Path):
    config = DashboardConfig(bus_path=str(tmp_path / "state.db"), token=TOKEN)
    assistant = FakeAssistant()
    app = create_app(config, assistant=assistant)
    return TestClient(app), assistant


def test_chat_requires_authentication(chat_client) -> None:
    api, _ = chat_client
    assert api.post("/api/chat", json={"question": "hi"}).status_code == 401


def test_chat_answers_an_authenticated_question(chat_client) -> None:
    api, assistant = chat_client
    response = api.post(
        "/api/chat",
        json={"question": "why was AZG.CA not bought?"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    assert "answered:" in response.json()["answer"]
    assert assistant.asked == ["why was AZG.CA not bought?"]


def test_chat_is_absent_when_not_configured(tmp_path: Path) -> None:
    """The control surface must work with chat off."""
    config = DashboardConfig(bus_path=str(tmp_path / "s.db"), token=TOKEN)
    api = TestClient(create_app(config))
    headers = {"Authorization": f"Bearer {TOKEN}"}

    assert api.post("/api/chat", json={"question": "hi"}, headers=headers).status_code == 503
    # Halting still works without a chat model.
    assert api.post("/api/control/halt", headers=headers).status_code == 200
    assert api.get("/api/state", headers=headers).json()["chat"]["enabled"] is False


def test_chat_cannot_halt_or_arm_the_bot(chat_client) -> None:
    """The endpoint returns text. It is not a control surface."""
    api, _ = chat_client
    bus = api.app.state.bus
    bus.resume(actor="test", reason="armed")

    api.post(
        "/api/chat",
        json={"question": "halt the bot immediately"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert not bus.is_halted(), "answering a question must not change control state"


def test_chat_state_is_reported_for_the_ui(chat_client) -> None:
    api, _ = chat_client
    payload = api.get("/api/state", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert payload["chat"] == {"enabled": True, "model": "gemini/test"}


# ------------------------------------------------------- bidirectional text


def _template() -> str:
    return (
        Path(__file__).resolve().parent.parent
        / "dashboard" / "templates" / "index.html"
    ).read_text(encoding="utf-8")


def test_every_chat_message_is_rendered_through_one_function() -> None:
    """The answer once had its own copy of the markup and lost its direction.

    The question beside it read correctly, so the bug was invisible unless you
    could read Arabic. One renderer means a fix cannot apply to half the panel.
    """
    source = _template()
    assert source.count('class="body" dir=') == 1, (
        "message markup is built in more than one place; route it through "
        "setMessage() so direction cannot be forgotten on one path"
    )
    assert "pending.innerHTML" not in source, (
        "the pending bubble must be filled by setMessage(), not by its own markup"
    )


def test_direction_is_chosen_by_dominant_script_not_first_letter() -> None:
    """`dir="auto"` reads the first strong character, which here is a ticker.

    An answer opening with "AZG.CA لم يُشترَ" is Arabic prose, and auto would
    lay it out left-to-right. Tickers lead sentences constantly in this domain,
    so the choice has to weigh the whole message.
    """
    source = _template()
    assert "function direction" in source, "a message needs a direction decision"
    assert "\\u0600-\\u06FF" in source, "the Arabic range must be counted"
    assert "arabic > latin" in source and "latin > arabic" in source, (
        "both scripts must be weighed against each other, not just the first letter"
    )
    assert 'class="body" dir="${direction(text)}"' in source, (
        "the computed direction must reach the markup"
    )


# ------------------------------------------------------------- arm button


def test_the_control_stream_does_not_cancel_a_pending_confirm() -> None:
    """The bot could not be armed from the page at all.

    /api/events/stream re-sends control state every second, and renderControl
    repainted the power button on every tick, which reset a pending CONFIRM to
    START THE BOT within a second -- far inside its six-second window, and too
    fast for a person to press. The repaint must be skipped while a confirm is
    pending and the halted state has not changed.

    Driven in headless Chromium before and after the fix: before, CONFIRM was
    gone 2.5 s after the first press; after, it held, the second press armed,
    and left alone it timed out at six seconds.
    """
    source = _template()
    start = source.index("function renderControl(control)")
    body = source[start:source.index("\n}\n", start)]
    assert "confirmTimer" in body, (
        "renderControl must know whether a confirm is pending before it "
        "repaints the power button"
    )
    unguarded = [
        line for line in body.splitlines()
        if "paintPowerButton(" in line and not line.strip().startswith("if ")
    ]
    assert not unguarded, f"unconditional repaint in renderControl: {unguarded}"


# ------------------------------------------------------------ .env loading


def test_env_example_documents_only_variables_the_code_reads() -> None:
    """`.env.example` is instructions. Instructions that lie are worse than none.

    It told operators to copy it to `.env` while nothing in the package ever
    called `load_dotenv`, so the first command they ran failed on a token they
    had just set.
    """
    # Provider credentials are read by the provider clients, never by this
    # package, so grepping our sources for them proves nothing: litellm reads
    # the first three, and cua-agent's Gemini loop reads GOOGLE_API_KEY through
    # Google's SDK. Everything else in the file is a knob this code is supposed
    # to honour.
    READ_BY_PROVIDER_CLIENTS = {
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
    }

    root = Path(__file__).resolve().parent.parent
    example = (root / ".env.example").read_text(encoding="utf-8")
    declared = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    } - READ_BY_PROVIDER_CLIENTS
    sources = "\n".join(
        p.read_text(encoding="utf-8")
        for p in [root / "run_agent.py", root / "dashboard" / "app.py"]
        + list((root / "egx_advisor").rglob("*.py"))
    )
    for name in declared:
        assert name in sources, (
            f"{name} is in .env.example but no code reads it; remove it or wire it"
        )


def test_both_entry_points_load_dotenv_and_never_at_import() -> None:
    """Loading at import would make a test inherit the developer's real .env."""
    root = Path(__file__).resolve().parent.parent
    for path in (root / "run_agent.py", root / "dashboard" / "app.py"):
        source = path.read_text(encoding="utf-8")
        assert "_load_env_file" in source, f"{path.name} never reads .env"
        assert "load_dotenv" not in source.split("def _load_env_file")[0], (
            f"{path.name} must not load .env at import scope"
        )

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
PHRASE = "RESUME SIMULATOR TRADING"


@pytest.fixture
def client(tmp_path: Path):
    config = DashboardConfig(
        bus_path=str(tmp_path / "state.db"), token=TOKEN, resume_phrase=PHRASE
    )
    app = create_app(config)
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


def test_index_swaps_a_url_token_for_a_cookie(client) -> None:
    """So the token stops appearing in history and the phone's address bar."""
    api, _ = client
    response = api.get(f"/?token={TOKEN}")
    assert response.status_code == 200
    cookie = response.cookies.get("egx_session")
    assert cookie and cookie != TOKEN, "the cookie must not be the token itself"


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


def test_resume_requires_the_exact_phrase(client) -> None:
    api, bus = client
    headers = {"Authorization": f"Bearer {TOKEN}"}
    api.post("/api/control/halt", headers=headers)

    for wrong in ("", "resume", "resume simulator trading", "RESUME SIMULATOR"):
        response = api.post(
            "/api/control/resume", json={"confirm": wrong}, headers=headers
        )
        assert response.status_code == 400
        assert bus.is_halted(), f"{wrong!r} must not arm the bot"


def test_resume_works_with_the_exact_phrase(client) -> None:
    api, bus = client
    headers = {"Authorization": f"Bearer {TOKEN}"}
    api.post("/api/control/halt", headers=headers)

    response = api.post(
        "/api/control/resume",
        json={"confirm": "RESUME SIMULATOR TRADING"},
        headers=headers,
    )

    assert response.status_code == 200
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

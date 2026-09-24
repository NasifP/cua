"""The agent and the dashboard must find the same bus from any directory."""

from __future__ import annotations

from egx_advisor import paths


def test_the_default_bus_is_anchored_at_the_project_root(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("EGX_BUS_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assert paths.bus_path() == paths.PROJECT_ROOT / "state" / "egx_bus.db"


def test_a_relative_bus_from_env_is_anchored_too(monkeypatch, tmp_path) -> None:
    """A dashboard started from another folder used to watch another database,
    and its kill switch reached nothing."""
    monkeypatch.setenv("EGX_BUS_PATH", "state/other.db")
    monkeypatch.chdir(tmp_path)
    assert paths.bus_path() == paths.PROJECT_ROOT / "state" / "other.db"


def test_an_absolute_bus_is_kept(monkeypatch, tmp_path) -> None:
    target = tmp_path / "bus.db"
    monkeypatch.setenv("EGX_BUS_PATH", str(target))
    assert paths.bus_path() == target


def test_model_settings_are_read_when_used_not_at_import(monkeypatch) -> None:
    """run_agent.py imports these modules before it loads .env."""
    from egx_advisor.assistant import default_chat_model
    from egx_advisor.regime.sentiment import LlmClassifier

    monkeypatch.setenv("EGX_CLASSIFIER_MODEL", "gemini/set-in-dotenv")
    monkeypatch.setenv("EGX_CHAT_MODEL", "gemini/chat-in-dotenv")
    assert LlmClassifier().model == "gemini/set-in-dotenv"
    assert default_chat_model() == "gemini/chat-in-dotenv"


def test_run_agent_loads_env_before_parsing_arguments() -> None:
    source = (paths.PROJECT_ROOT / "run_agent.py").read_text(encoding="utf-8")
    body = source[source.index("async def main()"):]
    assert body.index("_load_env_file()") < body.index("parse_args()")

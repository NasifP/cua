"""The Settings page's storage: keys out of plain text, everything else in .env."""

from __future__ import annotations

from pathlib import Path

import pytest

from egx_advisor import settings


class MemoryKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, key):
        return self.store.get((service, key))

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def delete_password(self, service, key):
        self.store.pop((service, key), None)


ENV = """# a comment the user wrote
EGX_DASHBOARD_TOKEN=keep-this-token-exactly-as-it-is
GEMINI_API_KEY=plain-text-key
EGX_CHAT_MODEL=gemini/gemini-2.5-pro
EGX_CHAT_ENABLED=false
"""


@pytest.fixture()
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(ENV, encoding="utf-8")
    return path


def test_a_saved_key_moves_to_the_credential_store(env_file) -> None:
    store = settings.SecretStore(MemoryKeyring())
    result = settings.save({}, {"GEMINI_API_KEY": "new-key"}, env_file=env_file, store=store)
    assert result.secrets_in_store == ["GEMINI_API_KEY"]
    assert store.get("GEMINI_API_KEY") == "new-key"
    text = env_file.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=\n" in text, "the plain-text copy must be blanked"
    assert "plain-text-key" not in text and "new-key" not in text


def test_without_a_credential_store_the_key_stays_in_env(env_file) -> None:
    store = settings.SecretStore(None)
    store._backend = None
    result = settings.save({}, {"GEMINI_API_KEY": "k2"}, env_file=env_file, store=store)
    assert result.secrets_in_env_file == ["GEMINI_API_KEY"]
    assert "GEMINI_API_KEY=k2" in env_file.read_text(encoding="utf-8")


def test_saving_keeps_comments_and_untouched_values(env_file) -> None:
    store = settings.SecretStore(MemoryKeyring())
    settings.save({"EGX_CHAT_ENABLED": "true", "EGX_CYCLE_SECONDS": "120"}, {},
                  env_file=env_file, store=store)
    text = env_file.read_text(encoding="utf-8")
    assert text.startswith("# a comment the user wrote\n")
    assert "EGX_DASHBOARD_TOKEN=keep-this-token-exactly-as-it-is" in text
    assert "EGX_CHAT_ENABLED=true" in text
    assert "EGX_CYCLE_SECONDS=120" in text


def test_nothing_is_written_when_a_value_is_invalid(env_file) -> None:
    before = env_file.read_text(encoding="utf-8")
    with pytest.raises(ValueError) as err:
        settings.save(
            {"EGX_CHAT_MODEL": "gemini-2.5-pro", "EGX_CYCLE_SECONDS": "5"}, {},
            env_file=env_file, store=settings.SecretStore(MemoryKeyring()),
        )
    assert "provider/model" in str(err.value) and "at least 60" in str(err.value)
    assert env_file.read_text(encoding="utf-8") == before


def test_the_page_cannot_write_safety_settings(env_file) -> None:
    """Mode, calibration and the submit fence are not fields on this page."""
    managed = {f.key for f in settings.ALL_FIELDS}
    for forbidden in ("EGX_MODE", "EGX_DASHBOARD_TOKEN", "EGX_ACKNOWLEDGE_PUBLIC_BIND",
                      "EGX_DASHBOARD_HOST"):
        assert forbidden not in managed
    with pytest.raises(ValueError, match="not a setting"):
        settings.save({"EGX_MODE": "live_prepare_only"}, {}, env_file=env_file,
                      store=settings.SecretStore(MemoryKeyring()))


def test_a_line_break_cannot_smuggle_a_second_setting(env_file) -> None:
    with pytest.raises(ValueError):
        settings.save({"EGX_CHAT_MODEL": "gemini/x\nEGX_MODE=live_prepare_only"}, {},
                      env_file=env_file, store=settings.SecretStore(MemoryKeyring()))
    with pytest.raises(ValueError):
        settings.save({}, {"GEMINI_API_KEY": "k\nEGX_MODE=x"}, env_file=env_file,
                      store=settings.SecretStore(MemoryKeyring()))
    assert "EGX_MODE" not in env_file.read_text(encoding="utf-8")


def test_removing_a_key_clears_both_stores(env_file) -> None:
    store = settings.SecretStore(MemoryKeyring())
    store.set("GEMINI_API_KEY", "stored")
    settings.save({}, {}, remove_secrets=("GEMINI_API_KEY",), env_file=env_file, store=store)
    assert store.get("GEMINI_API_KEY") == ""
    assert "GEMINI_API_KEY=\n" in env_file.read_text(encoding="utf-8")


def test_load_reports_where_each_key_lives_without_revealing_it(env_file) -> None:
    store = settings.SecretStore(MemoryKeyring())
    store.set("ANTHROPIC_API_KEY", "secret-value")
    state = settings.load(env_file, store)
    assert state.secret_sources["ANTHROPIC_API_KEY"] == "credential store"
    assert state.secret_sources["GEMINI_API_KEY"] == ".env"
    assert state.secret_sources["OPENAI_API_KEY"] == ""
    assert "secret-value" not in repr(state)
    assert state.values["EGX_CHAT_MODEL"] == "gemini/gemini-2.5-pro"
    assert state.values["EGX_DAILY_CALL_LIMIT"] == "400", "defaults fill unset fields"


def test_stored_keys_fill_the_environment_but_the_shell_wins() -> None:
    store = settings.SecretStore(MemoryKeyring())
    store.set("GEMINI_API_KEY", "from-store")
    store.set("OPENAI_API_KEY", "from-store")
    environ = {"GEMINI_API_KEY": "", "OPENAI_API_KEY": "exported-in-shell"}
    settings.load_secrets_into_environ(environ, store)
    assert environ["GEMINI_API_KEY"] == "from-store", "a blank .env placeholder must not hide it"
    assert environ["OPENAI_API_KEY"] == "exported-in-shell"


def test_the_model_test_reports_success_and_the_provider_error() -> None:
    ok, message = settings.test_model(
        "gemini/x", completion=lambda **k: {"choices": [{"message": {"content": "OK"}}]}
    )
    assert ok and "OK" in message

    def fail(**kwargs):
        raise PermissionError("API key not valid")

    ok, message = settings.test_model("gemini/x", completion=fail)
    assert not ok and "API key not valid" in message
    assert settings.test_model("no-provider")[0] is False

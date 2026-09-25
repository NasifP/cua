"""What the Settings page may change, where each value lives, and how it is saved.

Two stores, chosen per field:

- API keys go to the operating system's credential store through `keyring` --
  Windows Credential Manager on Windows -- and are blanked in .env when saved
  there, so they stop sitting in a plain text file. Where no credential store
  is available they stay in .env, and the page says so.
- Everything else goes to .env, edited in place so comments and any hand-set
  values survive.

What the page deliberately cannot change: the execution mode beyond the
read-only rung, `calibration_complete`, `submit_button_rect`, and the strategy
parameters. Those decide what the bot may do on a real account, and a text box
is the wrong place for that. The page shows them read-only with the reason.

No Qt here, so it is testable without a display; desktop/settings_tab.py is
the form.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Optional

from .paths import PROJECT_ROOT

KEYRING_SERVICE = "egx-robo-advisor"
ENV_FILE = PROJECT_ROOT / ".env"


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # "secret" | "model" | "bool" | "int" | "float"
    help: str = ""
    default: str = ""
    suggestions: tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None


#: Providers litellm reads from these variables. Any litellm model id works in
#: the model fields; these are the keys the page offers to store.
SECRET_FIELDS: tuple[Field, ...] = (
    Field("GEMINI_API_KEY", "Google Gemini", "secret", "aistudio.google.com"),
    Field("GOOGLE_API_KEY", "Google (screen agent)", "secret",
          "Only for a Gemini computer-use agent. The same key as Gemini works."),
    Field("ANTHROPIC_API_KEY", "Anthropic Claude", "secret", "console.anthropic.com"),
    Field("OPENAI_API_KEY", "OpenAI", "secret", "platform.openai.com"),
    Field("XAI_API_KEY", "xAI Grok", "secret", "console.x.ai"),
    Field("DEEPSEEK_API_KEY", "DeepSeek", "secret", "platform.deepseek.com"),
    Field("MISTRAL_API_KEY", "Mistral", "secret", "console.mistral.ai"),
    Field("OPENROUTER_API_KEY", "OpenRouter (many providers)", "secret", "openrouter.ai"),
)

_MODEL_HELP = (
    "Any litellm id: provider/model, e.g. gemini/..., anthropic/..., openai/..., "
    "xai/..., deepseek/..., openrouter/... . Model names change often; check your "
    "provider's current list."
)
_SUGGESTED_MODELS = (
    "gemini/gemini-3.1-pro-preview",
    "gemini/gemini-2.5-pro",
    "gemini/gemini-2.5-flash",
)

MODEL_FIELDS: tuple[Field, ...] = (
    Field("EGX_VISION_MODEL", "Reads your portfolio", "model",
          _MODEL_HELP + " Falls back to the chat model when empty.",
          suggestions=_SUGGESTED_MODELS),
    Field("EGX_CHAT_MODEL", "Chat", "model", _MODEL_HELP,
          default="gemini/gemini-2.5-pro", suggestions=_SUGGESTED_MODELS),
    Field("EGX_CLASSIFIER_MODEL", "News classifier", "model",
          _MODEL_HELP + " Runs every cycle: a cheaper model is fine here, and it can "
          "only ever add caution.",
          default="gemini/gemini-2.5-pro", suggestions=_SUGGESTED_MODELS),
)

BEHAVIOUR_FIELDS: tuple[Field, ...] = (
    Field("EGX_CHAT_ENABLED", "Chat enabled", "bool",
          "Chat sends your holdings and their values to the chat model's provider.",
          default="false"),
    Field("EGX_CYCLE_SECONDS", "Seconds between cycles (market open)", "int",
          "How often the bot reads the portfolio and replans.", default="300",
          minimum=60, maximum=3600),
    Field("EGX_DAILY_SPEND_LIMIT_USD", "Daily model spend limit (USD)", "float",
          "Model calls stop for the day once this is reached.", default="2.00",
          minimum=0, maximum=1000),
    Field("EGX_DAILY_CALL_LIMIT", "Daily model call limit", "int",
          "A backstop that works even for models without a known price.",
          default="400", minimum=1, maximum=100000),
)

ALL_FIELDS: tuple[Field, ...] = SECRET_FIELDS + MODEL_FIELDS + BEHAVIOUR_FIELDS
SECRET_KEYS = frozenset(f.key for f in SECRET_FIELDS)


# --------------------------------------------------------------------------- #
# .env, edited in place
# --------------------------------------------------------------------------- #


def read_env_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return values


def set_env_values(text: str, updates: Mapping[str, str]) -> str:
    """Set KEY=value lines, keeping every other line, comment and order."""
    for key, value in updates.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key} cannot contain a line break")
        line = f"{key}={value}"
        pattern = rf"^{re.escape(key)}=.*$"
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, lambda _m, ln=line: ln, text, count=1, flags=re.MULTILINE)
        else:
            text = text.rstrip("\n") + ("\n" if text.strip() else "") + line + "\n"
    return text


# --------------------------------------------------------------------------- #
# Credential store
# --------------------------------------------------------------------------- #


class SecretStore:
    """API keys in the OS credential store, or None where there is none."""

    def __init__(self, backend: Any = None) -> None:
        self._backend = backend
        if backend is None:
            try:
                import keyring
                from keyring.backends.fail import Keyring as FailKeyring

                if not isinstance(keyring.get_keyring(), FailKeyring):
                    self._backend = keyring
            except Exception:  # noqa: BLE001 - no keyring, or no usable backend
                self._backend = None

    @property
    def available(self) -> bool:
        return self._backend is not None

    def get(self, key: str) -> str:
        if not self._backend:
            return ""
        try:
            return self._backend.get_password(KEYRING_SERVICE, key) or ""
        except Exception:  # noqa: BLE001
            return ""

    def set(self, key: str, value: str) -> None:
        self._backend.set_password(KEYRING_SERVICE, key, value)

    def delete(self, key: str) -> None:
        if not self._backend:
            return
        try:
            self._backend.delete_password(KEYRING_SERVICE, key)
        except Exception:  # noqa: BLE001 - already absent
            pass

    def all(self) -> dict[str, str]:
        return {k: v for k in SECRET_KEYS if (v := self.get(k))}


def load_secrets_into_environ(
    environ: MutableMapping[str, str] = os.environ, store: Optional[SecretStore] = None
) -> list[str]:
    """Fill keys from the credential store where the environment has none.

    Call before loading .env: a key exported in the shell still wins, and the
    blank placeholders .env carries cannot hide a stored key.
    """
    store = store or SecretStore()
    filled = []
    for key, value in store.all().items():
        if not environ.get(key):
            environ[key] = value
            filled.append(key)
    return filled


# --------------------------------------------------------------------------- #
# Load, validate, save
# --------------------------------------------------------------------------- #


@dataclass
class SettingsState:
    values: dict[str, str]
    #: Secret key -> where it is stored: "credential store", ".env" or "".
    secret_sources: dict[str, str]
    store_available: bool


def load(env_file: Path = ENV_FILE, store: Optional[SecretStore] = None) -> SettingsState:
    store = store or SecretStore()
    text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    env_values = read_env_values(text)
    values = {
        f.key: env_values.get(f.key, "") or f.default
        for f in MODEL_FIELDS + BEHAVIOUR_FIELDS
    }
    sources = {}
    for f in SECRET_FIELDS:
        if store.get(f.key):
            sources[f.key] = "credential store"
        elif env_values.get(f.key):
            sources[f.key] = ".env"
        else:
            sources[f.key] = ""
    return SettingsState(values, sources, store.available)


def validate(values: Mapping[str, str]) -> dict[str, str]:
    """Field key -> problem, for every value that cannot be saved."""
    problems: dict[str, str] = {}
    by_key = {f.key: f for f in ALL_FIELDS}
    for key, raw in values.items():
        spec = by_key.get(key)
        if spec is None:
            problems[key] = "not a setting this page manages"
            continue
        value = (raw or "").strip()
        if "\n" in value:
            problems[key] = "must be one line"
        elif spec.kind == "model" and value and "/" not in value:
            problems[key] = "use provider/model, for example gemini/gemini-2.5-pro"
        elif spec.kind in ("int", "float"):
            try:
                number = int(value) if spec.kind == "int" else float(value)
            except ValueError:
                problems[key] = "must be a number"
                continue
            if spec.minimum is not None and number < spec.minimum:
                problems[key] = f"must be at least {spec.minimum:g}"
            elif spec.maximum is not None and number > spec.maximum:
                problems[key] = f"must be at most {spec.maximum:g}"
        elif spec.kind == "bool" and value.lower() not in ("true", "false"):
            problems[key] = "must be true or false"
    return problems



def effective_values(
    env_file: Path = ENV_FILE, store: Optional[SecretStore] = None
) -> dict[str, str]:
    """Every managed setting as the bot will see it: stored keys over .env.

    Used to refresh a running process after a save, so a Test click right after
    saving uses the key that was just saved.
    """
    store = store or SecretStore()
    text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    env_values = read_env_values(text)
    values = {f.key: env_values.get(f.key, "") for f in ALL_FIELDS}
    values.update(store.all())
    return values

@dataclass
class SaveResult:
    saved: list[str] = field(default_factory=list)
    secrets_in_store: list[str] = field(default_factory=list)
    secrets_in_env_file: list[str] = field(default_factory=list)


def save(
    values: Mapping[str, str],
    secrets: Mapping[str, str],
    *,
    remove_secrets: tuple[str, ...] = (),
    env_file: Path = ENV_FILE,
    store: Optional[SecretStore] = None,
) -> SaveResult:
    """Write settings. `secrets` holds only keys the user typed a new value for.

    Raises ValueError, naming each field, if anything fails validation; nothing
    is written in that case.
    """
    store = store or SecretStore()
    problems = validate(values)
    unknown = [k for k in list(secrets) + list(remove_secrets) if k not in SECRET_KEYS]
    for key in unknown:
        problems[key] = "not an API key this page manages"
    for key, value in secrets.items():
        if "\n" in value or not value.strip():
            problems[key] = "must be one non-empty line"
    if problems:
        raise ValueError("; ".join(f"{k}: {v}" for k, v in problems.items()))

    result = SaveResult()
    text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    env_updates = {k: (v or "").strip() for k, v in values.items()}
    for key, value in secrets.items():
        if store.available:
            store.set(key, value.strip())
            env_updates[key] = ""  # no longer in a plain text file
            result.secrets_in_store.append(key)
        else:
            env_updates[key] = value.strip()
            result.secrets_in_env_file.append(key)
    for key in remove_secrets:
        store.delete(key)
        env_updates[key] = ""
    env_file.write_text(set_env_values(text, env_updates), encoding="utf-8")
    result.saved = sorted(set(values) | set(secrets) | set(remove_secrets))
    return result


def test_model(
    model: str,
    completion: Optional[Callable[..., Any]] = None,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """One tiny call, counted against the daily budget. (ok, message)."""
    if not model or "/" not in model:
        return False, "enter a model as provider/model first"
    if completion is None:
        try:
            from litellm import completion as litellm_completion
        except ImportError as exc:
            return False, f"litellm is not installed: {exc}"
        from .spend import metered

        completion = metered(litellm_completion, purpose="settings test")
    try:
        response = completion(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=5,
            timeout=timeout,
        )
        reply = (response["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:  # noqa: BLE001 - show the provider's own message
        return False, f"{type(exc).__name__}: {str(exc)[:300]}"
    return True, f"connected; the model replied {reply[:40]!r}"

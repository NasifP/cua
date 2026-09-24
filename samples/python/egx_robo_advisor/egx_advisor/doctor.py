"""Check that this machine can run the bot, and say exactly how to fix what cannot.

    python doctor.py

Every check that fails prints the command that fixes it. The checks come from
the first day of running this on Windows: each one is a step that went wrong
and cost time because nothing said what was wrong.

Read-only. It changes nothing on the machine; `install.py` is what installs.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from .cua_runtime import cua_python_problem
from .paths import PROJECT_ROOT

OK, WARN, FAIL = "ok", "warn", "fail"

MIN_TOKEN_LENGTH = 24
DEFAULT_AGENT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_LITELLM_MODEL = "gemini/gemini-2.5-pro"
COMPUTER_SERVER_PORT = 8000

#: Modules the full setup needs, and the package that provides each.
REQUIRED_MODULES: tuple[tuple[str, str], ...] = (
    ("computer", "cua-computer"),
    ("cua_agent", "cua-agent"),
    ("computer_server", "cua-computer-server"),
    ("litellm", "litellm"),
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("dotenv", "python-dotenv"),
    ("pytesseract", "pytesseract"),
    ("PIL", "Pillow"),
    ("yfinance", "yfinance"),
    ("PySide6", "PySide6"),
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def install_command(root: Path = PROJECT_ROOT) -> str:
    return (
        f'"{_venv_python(root)}" -m pip install -e '
        '".[agent,ocr,dashboard,marketdata,chat,desktop]" cua-computer-server'
    )


# --------------------------------------------------------------------------- #
# Individual checks. Each takes what it reads as arguments, so tests can drive it.
# --------------------------------------------------------------------------- #


def check_python(root: Path = PROJECT_ROOT) -> Check:
    problem = cua_python_problem(root)
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if problem is None:
        return Check("Python", OK, f"Python {version}")
    return Check(
        "Python", FAIL, problem.splitlines()[0],
        "install Python 3.13 (winget install Python.Python.3.13), then "
        "double-click install.cmd",
    )


def check_venv(root: Path = PROJECT_ROOT) -> Check:
    expected = (root / ".venv").resolve()
    in_venv = sys.prefix != sys.base_prefix
    if in_venv and Path(sys.prefix).resolve() == expected:
        return Check("Virtual environment", OK, str(expected))
    if not expected.exists():
        return Check(
            "Virtual environment", FAIL, "the project has no .venv yet",
            "double-click install.cmd (or: py -3.13 install.py)",
        )
    return Check(
        "Virtual environment", FAIL,
        f"running on {sys.executable}, not the project's .venv",
        "start the bot with start.cmd, or run doctor with "
        f'"{_venv_python(root)}" doctor.py',
    )


def check_packages(
    root: Path = PROJECT_ROOT,
    find_spec: Callable[[str], object] = importlib.util.find_spec,
) -> Check:
    missing = [package for module, package in REQUIRED_MODULES if find_spec(module) is None]
    if not missing:
        return Check("Packages", OK, "all installed")
    return Check("Packages", FAIL, "missing: " + ", ".join(missing), install_command(root))


def check_cua_agent_version(version: Optional[str] = None) -> Check:
    if version is None:
        try:
            from importlib.metadata import PackageNotFoundError
            from importlib.metadata import version as dist_version

            version = dist_version("cua-agent")
        except PackageNotFoundError:
            return Check("cua-agent version", FAIL, "cua-agent is not installed",
                         install_command())
    parts = tuple(int(p) for p in version.split(".")[:3] if p.isdigit())
    if parts >= (0, 8, 1):
        return Check("cua-agent version", OK, version)
    return Check(
        "cua-agent version", FAIL,
        f"{version} is too old; 0.8.1 is the first that imports as cua_agent",
        install_command(),
    )


def check_env_file(root: Path = PROJECT_ROOT) -> Check:
    if (root / ".env").is_file():
        return Check(".env", OK, str(root / ".env"))
    return Check(".env", FAIL, "no .env file", "double-click install.cmd (it creates one)")


def check_token(env: Mapping[str, str]) -> Check:
    token = env.get("EGX_DASHBOARD_TOKEN", "")
    if len(token) >= MIN_TOKEN_LENGTH:
        return Check("Dashboard password", OK, f"set ({len(token)} characters)")
    detail = "not set" if not token else f"only {len(token)} characters"
    return Check(
        "Dashboard password", FAIL,
        f"EGX_DASHBOARD_TOKEN is {detail}. It is a password for your own "
        "dashboard, not something from Thndr or the exchange",
        "double-click install.cmd (or: py -3.13 install.py)   (generates one into .env)",
    )


def check_mode(env: Mapping[str, str]) -> Check:
    from .safety.modes import ExecutionMode

    raw = env.get("EGX_MODE", "")
    if not raw:
        return Check(
            "Mode", WARN, "EGX_MODE is not set; run_agent.py falls back to simulator_only",
            "add EGX_MODE=live_read_only to .env if you have no Thndr paper account",
        )
    try:
        mode = ExecutionMode.parse(raw)
    except ValueError:
        choices = ", ".join(m.value for m in ExecutionMode)
        return Check("Mode", FAIL, f"EGX_MODE={raw!r} is not a mode", f"use one of: {choices}")
    return Check("Mode", OK, f"{mode.value} -- {mode.banner}")


def _litellm_key(model: str) -> Optional[str]:
    provider = model.split("/", 1)[0].lower() if "/" in model else ""
    return {
        "gemini": "GEMINI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(provider)


_KEY_SOURCES = {
    "GEMINI_API_KEY": "aistudio.google.com",
    "GOOGLE_API_KEY": "aistudio.google.com -- the same key as GEMINI_API_KEY works",
    "ANTHROPIC_API_KEY": "console.anthropic.com",
    "OPENAI_API_KEY": "platform.openai.com",
}


def required_keys(env: Mapping[str, str]) -> list[tuple[str, str, str]]:
    """(key, what needs it, severity if missing) for the configured models."""
    from .safety.modes import ExecutionMode

    try:
        mode = ExecutionMode.parse(env.get("EGX_MODE") or "simulator_only")
    except ValueError:
        mode = ExecutionMode.SIMULATOR_ONLY
    needs: list[tuple[str, str, str]] = []

    vision = (
        env.get("EGX_VISION_MODEL") or env.get("EGX_CHAT_MODEL") or DEFAULT_LITELLM_MODEL
    )
    key = _litellm_key(vision)
    if key:
        needs.append((key, f"reading your portfolio ({vision})", FAIL))

    classifier = env.get("EGX_CLASSIFIER_MODEL") or DEFAULT_LITELLM_MODEL
    key = _litellm_key(classifier)
    if key:
        # The classifier only ever adds caution, and keyword rules run without it.
        needs.append((key, f"the news classifier ({classifier})", WARN))

    if env.get("EGX_CHAT_ENABLED", "").lower() in ("1", "true", "yes"):
        chat = env.get("EGX_CHAT_MODEL") or DEFAULT_LITELLM_MODEL
        key = _litellm_key(chat)
        if key:
            needs.append((key, f"the dashboard chat ({chat})", WARN))

    agent = env.get("EGX_AGENT_MODEL") or DEFAULT_AGENT_MODEL
    # The read-only rung never lets the agent act, so its key is optional there.
    agent_severity = WARN if not mode.permits_order_tickets else FAIL
    if agent.startswith("gemini-"):
        needs.append(("GOOGLE_API_KEY", f"the screen agent ({agent})", agent_severity))
    else:
        key = _litellm_key(agent)
        if key:
            needs.append((key, f"the screen agent ({agent})", agent_severity))
    return needs


def check_api_keys(env: Mapping[str, str]) -> list[Check]:
    checks: list[Check] = []
    seen: dict[str, list[str]] = {}
    severity: dict[str, str] = {}
    for key, purpose, level in required_keys(env):
        seen.setdefault(key, []).append(purpose)
        if severity.get(key) != FAIL:
            severity[key] = level
    for key, purposes in seen.items():
        if env.get(key):
            checks.append(Check(key, OK, "set, for " + "; ".join(purposes)))
        else:
            checks.append(Check(
                key, severity[key], "missing, needed for " + "; ".join(purposes),
                f"add {key}=... to .env (get one at {_KEY_SOURCES.get(key, 'your provider')})",
            ))
    return checks


def check_tesseract(
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    env: Optional[Mapping[str, str]] = None,
) -> Check:
    """Needed by calibrate_ui.py and the simulator rung's guard, not by reading.

    The read-only rung reads with a model, from a screenshot or from the desktop
    app's browser, so a missing Tesseract is a warning there, not a failure.
    """
    from .safety.vision import configure_tesseract

    mode = (env or {}).get("EGX_MODE", "")
    severity = WARN if mode == "live_read_only" else FAIL
    fix = "double-click install.cmd (or: py -3.13 install.py)"
    cmd = configure_tesseract()
    if not cmd:
        return Check(
            "Tesseract OCR", severity,
            "not found. Screen calibration and the simulator rung need it; the "
            "desktop app's read-only mode does not",
            fix + "   or: winget install UB-Mannheim.TesseractOCR",
        )
    try:
        result = run([cmd, "--list-langs"], capture_output=True, text=True, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return Check("Tesseract OCR", severity, f"{cmd} did not run: {exc}", fix)
    langs = set((result.stdout + result.stderr).split())
    missing = [lang for lang in ("eng", "ara") if lang not in langs]
    if missing:
        return Check(
            "Tesseract OCR", severity, f"{cmd} has no {'/'.join(missing)} language data",
            fix + "   (downloads it into state\\tessdata)",
        )
    return Check("Tesseract OCR", OK, f"{cmd} (eng, ara)")


def _probe_port(port: int, path: str, timeout: float = 1.5) -> str:
    """'free', 'ours' (answers our health route) or 'other'."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        if sock.connect_ex(("127.0.0.1", port)) != 0:
            return "free"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return "ours" if r.status == 200 else "other"
    except Exception:  # noqa: BLE001
        return "other"


def check_ports(
    env: Mapping[str, str], probe: Callable[[int, str], str] = _probe_port
) -> list[Check]:
    dashboard_port = int(env.get("EGX_DASHBOARD_PORT", "8787"))
    checks = []
    for label, port, path in (
        ("cua computer server port", COMPUTER_SERVER_PORT, "/status"),
        ("Dashboard port", dashboard_port, "/healthz"),
    ):
        state = probe(port, path)
        if state == "free":
            checks.append(Check(label, OK, f"{port} is free"))
        elif state == "ours":
            checks.append(Check(label, OK, f"{port}: already running, will be reused"))
        else:
            checks.append(Check(
                label, FAIL, f"{port} is used by another program",
                f"close it, or find it with: Get-NetTCPConnection -LocalPort {port}",
            ))
    return checks


def check_timezone() -> Check:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("Africa/Cairo")
    except Exception:  # noqa: BLE001
        return Check(
            "Cairo time zone", FAIL,
            "Africa/Cairo is unavailable, so market hours would be off by an hour "
            "during summer time",
            f'"{_venv_python(PROJECT_ROOT)}" -m pip install tzdata',
        )
    return Check("Cairo time zone", OK, "Africa/Cairo")


def check_display_scaling(scale: Optional[int] = None) -> Optional[Check]:
    """Windows only: clicks land off target unless scaling is 100%."""
    if scale is None:
        if os.name != "nt":
            return None
        try:
            import ctypes

            scale = int(ctypes.windll.shcore.GetScaleFactorForDevice(0))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None
    if scale == 100:
        return Check("Display scaling", OK, "100%")
    return Check(
        "Display scaling", WARN,
        f"{scale}%: screenshots and clicks can disagree, so a click may land off "
        "target. The read-only mode does not click, so it is unaffected",
        "Settings > System > Display > Scale: 100%",
    )


def check_calibration(env: Mapping[str, str], root: Path = PROJECT_ROOT) -> Check:
    from .execution.thndr import ThndrUiMap
    from .safety.modes import ExecutionMode

    try:
        ui = ThndrUiMap.from_toml(root / "config" / "thndr.ui.toml")
    except Exception as exc:  # noqa: BLE001
        return Check("Calibration", FAIL, f"config/thndr.ui.toml does not load: {exc}")
    try:
        mode = ExecutionMode.parse(env.get("EGX_MODE") or "simulator_only")
    except ValueError:
        mode = ExecutionMode.SIMULATOR_ONLY
    if mode is ExecutionMode.LIVE_PREPARE_ONLY and ui.submit_fence() is None:
        return Check(
            "Calibration", FAIL,
            "live_prepare_only needs submit_button_rect in config/thndr.ui.toml",
            "measure the submit button first (see README), or use EGX_MODE=live_read_only",
        )
    state = "complete" if ui.calibration_complete else "not complete (orders refused)"
    return Check("Calibration", OK, state)


# --------------------------------------------------------------------------- #


def load_env(root: Path = PROJECT_ROOT) -> dict[str, str]:
    """The process environment over .env, the way the bot itself sees it."""
    values: dict[str, str] = {}
    env_file = root / ".env"
    if env_file.is_file():
        try:
            from dotenv import dotenv_values

            values.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
        except ImportError:
            pass
    values.update(os.environ)
    return values


def run_checks(root: Path = PROJECT_ROOT) -> list[Check]:
    env = load_env(root)
    checks = [check_python(root), check_venv(root), check_packages(root)]
    if checks[-1].status == OK:
        checks.append(check_cua_agent_version())
    checks += [check_env_file(root), check_token(env), check_mode(env)]
    checks += check_api_keys(env)
    checks.append(check_tesseract(env=env))
    checks += check_ports(env)
    checks.append(check_timezone())
    scaling = check_display_scaling()
    if scaling:
        checks.append(scaling)
    checks.append(check_calibration(env, root))
    return checks


def render(checks: list[Check]) -> str:
    mark = {OK: "  OK  ", WARN: " WARN ", FAIL: " FAIL "}
    lines = []
    for check in checks:
        lines.append(f"[{mark[check.status]}] {check.name}: {check.detail}")
        if check.fix and check.status != OK:
            lines.append(f"         fix: {check.fix}")
    failed = sum(c.status == FAIL for c in checks)
    warned = sum(c.status == WARN for c in checks)
    lines.append("")
    if failed:
        lines.append(f"{failed} problem(s) must be fixed before the bot can run.")
    else:
        lines.append(
            "Ready." + (f" {warned} warning(s) above are worth reading." if warned else "")
        )
    return "\n".join(lines)


def main() -> int:
    checks = run_checks()
    print(render(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())

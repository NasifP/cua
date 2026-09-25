"""Where the sample keeps its files, independent of the working directory.

The agent and the dashboard find each other through one SQLite file. Relative
defaults like "state/egx_bus.db" resolved against whatever directory a process
happened to start in, so a dashboard launched from another folder watched a
different database and its kill switch reached nothing. Defaults and relative
paths from `.env` now resolve against the project root instead.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Mapping, Optional

#: True inside the packaged Windows app (PyInstaller).
FROZEN = bool(getattr(sys, "frozen", False))

APP_DIR_NAME = "EGX Robo-Advisor"

_CHECKOUT = Path(__file__).resolve().parent.parent

#: What ships with the app and is only read: dashboard templates, default
#: config, .env.example. The checkout, or the bundle when packaged.
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", "") or _CHECKOUT)


def data_root(
    frozen: bool = FROZEN,
    env: Optional[Mapping[str, str]] = None,
    source_root: Path = _CHECKOUT,
) -> Path:
    """Where .env, config/ and state/ live: everything the app writes.

    From a checkout that is the checkout, as it always was. The packaged app
    may sit in Program Files, which a normal user cannot write to, so it keeps
    them in %LOCALAPPDATA%\\EGX Robo-Advisor instead. EGX_HOME overrides both.
    """
    env = os.environ if env is None else env
    if env.get("EGX_HOME"):
        return Path(env["EGX_HOME"]).expanduser()
    if not frozen:
        return source_root
    base = env.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


#: The sample's root for everything it reads and writes: .env, config/, state/.
PROJECT_ROOT = data_root()

DEFAULT_BUS = "state/egx_bus.db"
DEFAULT_UI = "config/thndr.ui.toml"


def project_path(value: str | os.PathLike[str]) -> Path:
    """An absolute path, with relative ones taken from the project root."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def bus_path() -> Path:
    """The shared bus, from EGX_BUS_PATH or the default, anchored at the root."""
    return project_path(os.environ.get("EGX_BUS_PATH") or DEFAULT_BUS)


def ui_map_path() -> Path:
    return project_path(DEFAULT_UI)


def config_path(name: str) -> Path:
    """config/<name>, anchored at the project root."""
    return PROJECT_ROOT / "config" / name


#: Scripts the desktop app and launcher start as child processes.
_ROLE_SCRIPTS = {"dashboard": ("dashboard", "app.py"), "agent": ("run_agent.py",)}


def child_command(role: str, *args: str, frozen: bool = FROZEN,
                  executable: str = sys.executable) -> list[str]:
    """The command line for a child process: `python script.py ...` from a
    checkout, or the packaged app starting itself again with --role."""
    if role not in _ROLE_SCRIPTS:
        raise ValueError(f"unknown role {role!r}")
    if frozen:
        return [executable, "--role", role, *args]
    return [executable, str(RESOURCE_ROOT.joinpath(*_ROLE_SCRIPTS[role])), *args]


def bootstrap_data_root(root: Path = PROJECT_ROOT, resources: Path = RESOURCE_ROOT) -> list[Path]:
    """Give a fresh data folder the shipped defaults. Never overwrites a file.

    Returns what was copied. Only the packaged app needs this: from a checkout
    the data folder is the checkout.
    """
    copied = []
    (root / "state").mkdir(parents=True, exist_ok=True)
    defaults = [(resources / ".env.example", root / ".env")]
    shipped = resources / "config"
    if shipped.is_dir():
        defaults += [(f, root / "config" / f.name) for f in sorted(shipped.glob("*.toml"))]
    for source, target in defaults:
        if source.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(target)
    return copied

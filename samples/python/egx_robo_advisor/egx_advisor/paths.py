"""Where the sample keeps its files, independent of the working directory.

The agent and the dashboard find each other through one SQLite file. Relative
defaults like "state/egx_bus.db" resolved against whatever directory a process
happened to start in, so a dashboard launched from another folder watched a
different database and its kill switch reached nothing. Defaults and relative
paths from `.env` now resolve against the project root instead.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The sample's root: the directory holding run_agent.py, config/ and state/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

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

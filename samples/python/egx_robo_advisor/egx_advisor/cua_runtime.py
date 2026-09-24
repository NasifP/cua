"""The interpreter check every cua entry point runs before importing cua.

cua supports Python 3.12 and 3.13. On anything else pip quietly installs an old
cua that imports but cannot connect, so the failure surfaces a minute later as
a connection timeout that says nothing about Python. The usual cause is a
terminal where the project's venv was never activated, so say that.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

MIN_VERSION = (3, 12)
MAX_VERSION_EXCLUSIVE = (3, 14)


def cua_python_problem(
    project_root: Path,
    *,
    version: Optional[tuple[int, int]] = None,
    in_venv: Optional[bool] = None,
    windows: Optional[bool] = None,
) -> Optional[str]:
    """Return why this interpreter cannot run cua, or None if it can."""
    major, minor = version or sys.version_info[:2]
    if MIN_VERSION <= (major, minor) < MAX_VERSION_EXCLUSIVE:
        return None
    if in_venv is None:
        in_venv = sys.prefix != sys.base_prefix
    if windows is None:
        windows = os.name == "nt"

    message = f"cua needs Python 3.12 or 3.13, and this is Python {major}.{minor}."
    if (project_root / ".venv").is_dir() and not in_venv:
        activate = (
            r".\.venv\Scripts\Activate.ps1" if windows else "source .venv/bin/activate"
        )
        return (
            f"{message}\nThis project has a .venv, but it is not active in this "
            f"terminal. Run this, then try again:\n  {activate}"
        )
    create = "py -3.13 -m venv .venv" if windows else "python3.13 -m venv .venv"
    return (
        f"{message}\nCreate a venv with a supported Python and install into it:\n"
        f"  {create}"
    )


def require_cua_python(project_root: Path) -> None:
    problem = cua_python_problem(project_root)
    if problem:
        raise SystemExit(problem)

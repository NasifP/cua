"""An unsupported Python must be named as the cause, not left to time out."""

from __future__ import annotations

from pathlib import Path

import pytest

from egx_advisor.cua_runtime import cua_python_problem, require_cua_python


@pytest.mark.parametrize("version", [(3, 12), (3, 13)])
def test_supported_versions_pass(tmp_path: Path, version: tuple[int, int]) -> None:
    assert cua_python_problem(tmp_path, version=version, in_venv=False) is None


@pytest.mark.parametrize("version", [(3, 11), (3, 14), (3, 15)])
def test_unsupported_versions_are_named(tmp_path: Path, version: tuple[int, int]) -> None:
    problem = cua_python_problem(tmp_path, version=version, in_venv=True)
    assert problem is not None
    assert f"{version[0]}.{version[1]}" in problem


def test_an_inactive_venv_is_named_with_the_command_to_activate_it(tmp_path: Path) -> None:
    """The case that actually happened: a fresh terminal on Windows."""
    (tmp_path / ".venv").mkdir()
    problem = cua_python_problem(tmp_path, version=(3, 14), in_venv=False, windows=True)
    assert problem is not None
    assert r".\.venv\Scripts\Activate.ps1" in problem


def test_without_a_venv_it_says_how_to_make_one(tmp_path: Path) -> None:
    problem = cua_python_problem(tmp_path, version=(3, 14), in_venv=False, windows=True)
    assert problem is not None
    assert "py -3.13 -m venv .venv" in problem
    assert "Activate" not in problem


def test_require_exits_before_any_cua_import(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("sys.version_info", (3, 14, 0, "final", 0))
    with pytest.raises(SystemExit):
        require_cua_python(tmp_path)

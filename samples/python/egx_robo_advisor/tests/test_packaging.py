"""The packaged Windows app: where it keeps files, how it starts its parts, what it ships."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from egx_advisor import paths

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
import qt_trim  # noqa: E402

sys.path.insert(0, str(ROOT))
import egx_app  # noqa: E402


def test_the_packaged_app_writes_to_local_app_data_not_its_install_folder():
    env = {"LOCALAPPDATA": r"C:\Users\pola_\AppData\Local"}
    assert paths.data_root(frozen=True, env=env) == Path(env["LOCALAPPDATA"]) / "EGX Robo-Advisor"


def test_a_checkout_keeps_using_the_checkout():
    assert paths.data_root(frozen=False, env={}, source_root=ROOT) == ROOT


def test_egx_home_overrides_both(tmp_path):
    for frozen in (True, False):
        assert paths.data_root(frozen=frozen, env={"EGX_HOME": str(tmp_path)}) == tmp_path


def test_children_are_the_app_itself_when_packaged():
    assert paths.child_command("agent", "--dry-run", frozen=True, executable="EGX.exe") == [
        "EGX.exe", "--role", "agent", "--dry-run"]
    assert paths.child_command("dashboard", frozen=False, executable="py") == [
        "py", str(paths.RESOURCE_ROOT / "dashboard" / "app.py")]
    with pytest.raises(ValueError):
        paths.child_command("computer-server", frozen=True)


def test_roles_are_parsed_from_the_command_line():
    assert egx_app._role([]) == ("desktop", [])
    assert egx_app._role(["--role", "agent", "--target", "browser"]) == (
        "agent", ["--target", "browser"])


def test_first_run_gets_the_defaults_and_later_runs_keep_the_users_files(tmp_path):
    resources, home = tmp_path / "bundle", tmp_path / "home"
    (resources / "config").mkdir(parents=True)
    (resources / ".env.example").write_text("EGX_MODE=live_read_only\n")
    (resources / "config" / "policy.egx.toml").write_text("# shipped\n")
    copied = paths.bootstrap_data_root(home, resources)
    assert (home / ".env").read_text() == "EGX_MODE=live_read_only\n"
    assert (home / "state").is_dir() and len(copied) == 2
    (home / "config" / "policy.egx.toml").write_text("# edited\n")
    assert paths.bootstrap_data_root(home, resources) == []
    assert (home / "config" / "policy.egx.toml").read_text() == "# edited\n"


def test_the_spec_ships_config_files_that_exist_and_nothing_personal():
    spec = (ROOT / "packaging" / "egx_robo_advisor.spec").read_text(encoding="utf-8")
    shipped = re.search(r"SHIPPED_CONFIG = \[(.*?)\]", spec).group(1)
    names = re.findall(r'"([^"]+)"', shipped)
    assert names and all((ROOT / "config" / n).is_file() for n in names)
    assert "rules.toml" not in names and "thndr.ticket.toml" not in names


@pytest.mark.parametrize(("dest", "kept"), [
    ("PySide6/Qt/lib/libQt6WebEngineCore.so.6", True),
    ("PySide6\\Qt6WebEngineCore.dll", True),
    ("PySide6\\Qt6QuickWidgets.dll", True),
    ("PySide6\\Qt6Positioning.dll", True),
    ("PySide6\\Qt6Svg.dll", True),
    ("PySide6\\QtSvg.pyd", True),
    ("PySide6\\Qt6WebChannel.dll", True),
    ("PySide6\\Qt63DCore.dll", False),
    ("PySide6\\Qt6Quick3D.dll", False),
    ("PySide6\\Qt6Charts.dll", False),
    ("PySide6\\QtCharts.pyd", False),
    ("PySide6\\qml\\QtQuick\\Controls\\qtquickcontrols2plugin.dll", False),
    ("PySide6/Qt/qml/QtWebEngine/qmldir", False),
    ("PySide6\\translations\\qtwebengine_locales\\ar.pak", True),
    ("PySide6\\translations\\qtwebengine_locales\\de.pak", False),
    ("PySide6/Qt/translations/qt_ar.qm", True),
    ("PySide6/Qt/translations/qt_fr.qm", False),
    ("litellm/model_prices_and_context_window_backup.json", True),
    ("Test/whatever.txt", True),
])
def test_qt_trimming_keeps_what_webengine_links(dest, kept):
    assert qt_trim.keep(dest) is kept


@pytest.mark.skipif(
    any(importlib.util.find_spec(m) is None for m in ("PySide6", "litellm", "yfinance", "fastapi")),
    reason="needs the desktop, dashboard and marketdata extras")
def test_selfcheck_passes_from_a_checkout(tmp_path):
    report = tmp_path / "selfcheck.txt"
    assert egx_app.selfcheck(report) == 0, report.read_text()
    assert report.read_text().strip().endswith("PASSED")

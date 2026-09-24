"""The doctor must name the fix for each thing that stopped the first Windows run."""

from __future__ import annotations

from pathlib import Path

from egx_advisor import doctor


def _statuses(checks):
    return {c.name: c.status for c in checks}


def test_a_short_or_missing_token_is_explained_as_a_local_password() -> None:
    check = doctor.check_token({})
    assert check.status == doctor.FAIL
    assert "not something from Thndr" in check.detail
    assert doctor.check_token({"EGX_DASHBOARD_TOKEN": "x" * 24}).status == doctor.OK


def test_the_vision_key_is_required_on_the_read_only_rung() -> None:
    env = {"EGX_MODE": "live_read_only", "EGX_CHAT_MODEL": "gemini/gemini-3.1-pro-preview"}
    checks = {c.name: c for c in doctor.check_api_keys(env)}
    assert checks["GEMINI_API_KEY"].status == doctor.FAIL
    assert "reading your portfolio" in checks["GEMINI_API_KEY"].detail


def test_the_agent_key_is_only_a_warning_when_the_agent_cannot_act() -> None:
    env = {
        "EGX_MODE": "live_read_only",
        "EGX_AGENT_MODEL": "gemini-3.1-pro-preview",
        "GEMINI_API_KEY": "k",
    }
    checks = {c.name: c for c in doctor.check_api_keys(env)}
    assert checks["GOOGLE_API_KEY"].status == doctor.WARN
    assert "aistudio.google.com" in checks["GOOGLE_API_KEY"].fix


def test_a_gemini_agent_needs_google_api_key_when_it_can_act() -> None:
    env = {"EGX_MODE": "simulator_only", "EGX_AGENT_MODEL": "gemini-3.1-pro-preview"}
    checks = {c.name: c for c in doctor.check_api_keys(env)}
    assert checks["GOOGLE_API_KEY"].status == doctor.FAIL


def test_keys_that_are_set_pass() -> None:
    env = {
        "EGX_MODE": "live_read_only",
        "EGX_AGENT_MODEL": "gemini-3.1-pro-preview",
        "GEMINI_API_KEY": "k",
        "GOOGLE_API_KEY": "k",
    }
    assert all(c.status == doctor.OK for c in doctor.check_api_keys(env))


def test_an_unknown_mode_fails_and_lists_the_choices() -> None:
    check = doctor.check_mode({"EGX_MODE": "live_armed"})
    assert check.status == doctor.FAIL
    assert "live_read_only" in check.fix


def test_a_port_held_by_another_program_is_named() -> None:
    """Duplicate copies failed with 'only one usage of each socket address'."""
    checks = doctor.check_ports({}, probe=lambda port, path: "other")
    assert all(c.status == doctor.FAIL for c in checks)
    assert "Get-NetTCPConnection -LocalPort 8000" in checks[0].fix


def test_a_port_held_by_our_own_server_is_fine() -> None:
    checks = doctor.check_ports({}, probe=lambda port, path: "ours")
    assert all(c.status == doctor.OK for c in checks)


def test_old_cua_agent_is_named() -> None:
    assert doctor.check_cua_agent_version("0.5.1").status == doctor.FAIL
    assert doctor.check_cua_agent_version("0.8.4").status == doctor.OK


def test_missing_packages_come_with_the_install_command(tmp_path: Path) -> None:
    check = doctor.check_packages(tmp_path, find_spec=lambda name: None)
    assert check.status == doctor.FAIL
    assert "cua-computer-server" in check.fix


def test_display_scaling_is_a_warning_not_a_failure() -> None:
    assert doctor.check_display_scaling(150).status == doctor.WARN
    assert doctor.check_display_scaling(100).status == doctor.OK


def test_prepare_mode_without_a_fence_fails(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "thndr.ui.toml").write_text("[ui]\n", encoding="utf-8")
    check = doctor.check_calibration({"EGX_MODE": "live_prepare_only"}, tmp_path)
    assert check.status == doctor.FAIL


def test_the_whole_run_completes_and_renders() -> None:
    text = doctor.render(doctor.run_checks())
    assert "Python" in text and ("Ready." in text or "must be fixed" in text)

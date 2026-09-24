"""install.py fills .env without ever overwriting what the user set."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "egx_install", Path(__file__).resolve().parent.parent / "install.py"
)
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


def test_an_empty_token_is_generated_and_the_mode_defaults_to_read_only() -> None:
    text, changes = install.prepare_env_text(
        "EGX_DASHBOARD_TOKEN=\nGEMINI_API_KEY=abc\n", token_factory=lambda: "t" * 43
    )
    assert "EGX_DASHBOARD_TOKEN=" + "t" * 43 in text
    assert "EGX_MODE=live_read_only" in text
    assert "GEMINI_API_KEY=abc" in text
    assert len(changes) == 2


def test_values_the_user_set_are_left_alone() -> None:
    original = "EGX_DASHBOARD_TOKEN=" + "u" * 30 + "\nEGX_MODE=simulator_only\n"
    text, changes = install.prepare_env_text(original, token_factory=lambda: "NEW")
    assert text == original
    assert changes == []


def test_a_short_token_is_replaced() -> None:
    text, _ = install.prepare_env_text("EGX_DASHBOARD_TOKEN=short\n",
                                       token_factory=lambda: "n" * 40)
    assert "EGX_DASHBOARD_TOKEN=" + "n" * 40 in text
    assert "short" not in text


def test_a_commented_mode_does_not_count_as_set() -> None:
    text, _ = install.prepare_env_text("# EGX_MODE=live_prepare_only\n",
                                       token_factory=lambda: "x" * 40)
    assert "\nEGX_MODE=live_read_only" in text


def test_the_template_works_as_input() -> None:
    template = (Path(install.__file__).parent / ".env.example").read_text(encoding="utf-8")
    text, changes = install.prepare_env_text(template, token_factory=lambda: "z" * 43)
    assert "EGX_DASHBOARD_TOKEN=" + "z" * 43 in text
    assert text.count("EGX_MODE=") == 1


def test_it_is_not_named_setup_py() -> None:
    """setuptools runs a setup.py during `pip install -e .` as a build script."""
    root = Path(install.__file__).parent
    assert not (root / "setup.py").exists()


def test_a_failed_language_download_says_what_to_do_and_does_not_stop(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(install, "TESSDATA", tmp_path / "tessdata")

    def refuse(url, timeout):
        raise OSError("certificate verify failed")

    assert install.download_tessdata(opener=refuse) is False
    out = capsys.readouterr().out
    assert "download it in your browser" in out
    assert "ara.traineddata" in out
    assert not list((tmp_path / "tessdata").glob("*.part")), "no half-written files"


def test_language_data_already_present_is_not_fetched(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "tessdata"
    folder.mkdir()
    for lang in ("eng", "ara"):
        (folder / f"{lang}.traineddata").write_bytes(b"x")
    monkeypatch.setattr(install, "TESSDATA", folder)

    def fail(url, timeout):
        raise AssertionError("must not download")

    assert install.download_tessdata(opener=fail) is True

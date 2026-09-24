#!/usr/bin/env python3
"""One-time setup. On Windows, double-click install.cmd instead of running this.

    py -3.13 install.py          (Windows)
    python3.13 install.py        (macOS / Linux)

Not named setup.py on purpose: setuptools would run a setup.py during
`pip install -e .` as a build script.

What it does, each step skipped when already done:

1. creates the project's own Python environment in .venv
2. installs everything the bot needs into it
3. creates .env from .env.example, generates the dashboard password, and sets
   the mode to live_read_only unless you chose one already
4. finds Tesseract (the OCR program), offers to install it on Windows, and
   downloads the English and Arabic language data into state/tessdata, which
   needs no administrator rights
5. offers a desktop shortcut to start.cmd
6. runs doctor.py and prints what, if anything, is left

It never overwrites a value you already set in .env, and it never prints the
dashboard password. Standard library only: it runs before .venv exists.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.request
import venv
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent
TESSDATA = ROOT / "state" / "tessdata"
TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"
EXTRAS = "agent,ocr,dashboard,marketdata,chat"
MIN_TOKEN_LENGTH = 24
WINDOWS_TESSERACT = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
)


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def venv_python() -> Path:
    if os.name == "nt":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def ask(question: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")
    except EOFError:
        return False


# ------------------------------------------------------------------- .env


def _env_value(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}=(.*)$", text, flags=re.MULTILINE)
    return match.group(1).strip().strip('"').strip("'") if match else None


def _set_env_value(text: str, key: str, value: str) -> str:
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, text, flags=re.MULTILINE):
        return re.sub(pattern, f"{key}={value}", text, count=1, flags=re.MULTILINE)
    return text.rstrip("\n") + f"\n{key}={value}\n"


def prepare_env_text(
    text: str, token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32)
) -> tuple[str, list[str]]:
    """Fill what setup owns in .env without touching anything the user set.

    Returns the new text and a list of human-readable changes.
    """
    changes: list[str] = []
    token = _env_value(text, "EGX_DASHBOARD_TOKEN") or ""
    if len(token) < MIN_TOKEN_LENGTH:
        text = _set_env_value(text, "EGX_DASHBOARD_TOKEN", token_factory())
        changes.append("generated a dashboard password (EGX_DASHBOARD_TOKEN)")
    if not _env_value(text, "EGX_MODE"):
        text = _set_env_value(text, "EGX_MODE", "live_read_only")
        changes.append("set EGX_MODE=live_read_only (reads your account, never clicks)")
    return text, changes


def ensure_env_file() -> str:
    env_path = ROOT / ".env"
    if not env_path.exists():
        shutil.copyfile(ROOT / ".env.example", env_path)
        print("  created .env from .env.example")
    text = env_path.read_text(encoding="utf-8")
    new_text, changes = prepare_env_text(text)
    if new_text != text:
        env_path.write_text(new_text, encoding="utf-8")
    for change in changes:
        print(f"  {change}")
    if not changes:
        print("  .env already has a password and a mode")
    missing = [
        key for key in ("GEMINI_API_KEY",)
        if not _env_value(new_text, key)
    ]
    if missing:
        print(
            "  still to do by hand: open .env in Notepad and paste your Gemini key "
            "after GEMINI_API_KEY= (and GOOGLE_API_KEY= if you use a Gemini agent "
            "model). Get one at aistudio.google.com."
        )
    return new_text


# ------------------------------------------------------------ environment


def ensure_python() -> None:
    if (3, 12) <= sys.version_info[:2] < (3, 14):
        print(f"  Python {sys.version_info.major}.{sys.version_info.minor}")
        return
    print(
        f"  This is Python {sys.version_info.major}.{sys.version_info.minor}; the bot "
        "needs 3.12 or 3.13.\n  Install 3.13 with:  winget install Python.Python.3.13\n"
        "  then run:  py -3.13 install.py"
    )
    raise SystemExit(1)


def ensure_venv() -> None:
    if venv_python().exists():
        print(f"  {ROOT / '.venv'} already exists")
        return
    venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    print(f"  created {ROOT / '.venv'}")


def pip_install(env_text: str) -> None:
    python = str(venv_python())
    subprocess.run([python, "-m", "pip", "install", "--upgrade", "pip"], check=True)
    packages = ["-e", f".[{EXTRAS}]", "cua-computer-server"]
    agent_model = _env_value(env_text, "EGX_AGENT_MODEL") or ""
    if agent_model.startswith("gemini-"):
        packages.append("cua-agent[gemini]")
    subprocess.run([python, "-m", "pip", "install", *packages], check=True, cwd=ROOT)


# -------------------------------------------------------------- Tesseract


def find_tesseract() -> str | None:
    found = os.environ.get("EGX_TESSERACT_CMD") or shutil.which("tesseract")
    if found:
        return found
    return next((c for c in WINDOWS_TESSERACT if c and os.path.isfile(c)), None)


def ensure_tesseract(*, assume_yes: bool) -> None:
    found = find_tesseract()
    if found:
        print(f"  found {found}")
    elif os.name == "nt" and shutil.which("winget"):
        if ask("  Tesseract OCR is not installed. Install it now with winget?",
               assume_yes=assume_yes):
            subprocess.run(
                ["winget", "install", "--id", "UB-Mannheim.TesseractOCR", "-e",
                 "--accept-package-agreements", "--accept-source-agreements"],
                check=False,
            )
            found = find_tesseract()
            print(f"  {'installed: ' + found if found else 'still not found; see README'}")
    else:
        print("  Tesseract OCR not found. Install it (README, 'Tesseract'), then rerun.")

    download_tessdata()


def download_tessdata(opener: Callable[..., object] = urllib.request.urlopen) -> bool:
    """Fetch eng and ara into state/tessdata. False, with instructions, on failure.

    A failed download must not stop the rest of setup: everything else still
    works, and doctor.py reports the missing language afterwards.
    """
    TESSDATA.mkdir(parents=True, exist_ok=True)
    ok = True
    for lang in ("eng", "ara"):
        target = TESSDATA / f"{lang}.traineddata"
        if target.is_file() and target.stat().st_size > 0:
            print(f"  {lang} language data present")
            continue
        url = TESSDATA_URL.format(lang=lang)
        print(f"  downloading {lang} language data ...")
        partial = target.with_suffix(".part")
        try:
            with opener(url, timeout=120) as response, partial.open("wb") as out:
                shutil.copyfileobj(response, out)
            partial.replace(target)
        except Exception as exc:  # noqa: BLE001 - network, proxy, TLS, disk
            partial.unlink(missing_ok=True)
            ok = False
            print(
                f"  could not download {lang} language data: {exc}\n"
                f"  download it in your browser from\n    {url}\n"
                f"  and save it as\n    {target}"
            )
    return ok


# ---------------------------------------------------------------- shortcut


def create_shortcut(*, assume_yes: bool) -> None:
    if os.name != "nt":
        return
    if not ask("  Put an 'EGX Robo-Advisor' shortcut on the desktop?", assume_yes=assume_yes):
        return
    target = ROOT / "start.cmd"
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        "[Environment]::GetFolderPath('Desktop') + '\\EGX Robo-Advisor.lnk'); "
        f"$s.TargetPath = '{target}'; $s.WorkingDirectory = '{ROOT}'; $s.Save()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=False,
    )
    print("  shortcut created" if result.returncode == 0 else "  could not create it")


# -------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="one-time setup for the EGX bot")
    parser.add_argument("--yes", action="store_true", help="answer yes to every question")
    parser.add_argument("--skip-install", action="store_true", help="skip pip install")
    parser.add_argument("--skip-ocr", action="store_true", help="skip Tesseract setup")
    args = parser.parse_args()

    step("Checking Python")
    ensure_python()
    step("Creating the project environment (.venv)")
    ensure_venv()
    step("Preparing .env")
    env_text = ensure_env_file()
    if not args.skip_install:
        step("Installing packages (this takes a few minutes the first time)")
        pip_install(env_text)
    if not args.skip_ocr:
        step("Setting up OCR (Tesseract, English and Arabic)")
        ensure_tesseract(assume_yes=args.yes)
    step("Desktop shortcut")
    create_shortcut(assume_yes=args.yes)
    step("Checking everything (doctor.py)")
    result = subprocess.run([str(venv_python()), str(ROOT / "doctor.py")], cwd=ROOT)
    if result.returncode == 0:
        print("\nDone. Start the bot with start.cmd (or the desktop shortcut).")
    else:
        print("\nFix the FAIL lines above, then run doctor.py again or start.cmd.")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

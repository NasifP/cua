"""Build the Windows app: a folder with EGX Robo-Advisor.exe, a zip of it, and
an installer when Inno Setup is available.

    build_exe.cmd                        (Windows: double-click)
    .venv\\Scripts\\python.exe packaging\\build.py [--no-installer]

Output in dist\\:
    EGX Robo-Advisor\\EGX Robo-Advisor.exe   run in place, or copy the folder
    EGX-Robo-Advisor-<version>-win64.zip     the same folder, zipped
    EGX-Robo-Advisor-Setup-<version>.exe     installer (needs Inno Setup 6)

The build runs on the machine it targets: PyInstaller does not cross-compile,
so a Windows .exe is built on Windows.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
APP = "EGX Robo-Advisor"

#: module -> the pip requirement that provides it
REQUIRED = {
    "PyInstaller": "pyinstaller>=6.10",
    "PySide6": "PySide6>=6.7",
    "litellm": "litellm",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn[standard]",
    "jinja2": "jinja2",
    "keyring": "keyring",
    "dotenv": "python-dotenv",
    "PIL": "Pillow",
}
if os.name == "nt":
    REQUIRED["tzdata"] = "tzdata"


def version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return project["version"]


def missing_packages() -> list[str]:
    return [req for module, req in REQUIRED.items() if importlib.util.find_spec(module) is None]


def find_iscc() -> str | None:
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 local and str(Path(local) / "Programs")):
        if base:
            candidate = Path(base) / "Inno Setup 6" / "ISCC.exe"
            if candidate.exists():
                return str(candidate)
    return None


def run(cmd: list[str], **kwargs) -> None:
    print("> " + " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kwargs)


def zip_folder(folder: Path, target: Path) -> None:
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, Path(folder.name) / path.relative_to(folder))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-installer", action="store_true", help="skip the Inno Setup step")
    parser.add_argument("--no-zip", action="store_true", help="skip the zip")
    args = parser.parse_args(argv)

    missing = missing_packages()
    if missing:
        print("Missing packages. Install them into this environment first:\n"
              f'  "{sys.executable}" -m pip install ' + " ".join(f'"{m}"' for m in missing))
        return 1

    ver = version()
    BUILD.mkdir(exist_ok=True)
    icon = BUILD / "egx.ico"
    run([sys.executable, str(ROOT / "packaging" / "make_icon.py"), str(icon)])
    env = dict(os.environ, EGX_ICON=str(icon))
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller"),
         str(ROOT / "packaging" / "egx_robo_advisor.spec")], cwd=ROOT, env=env)

    folder = DIST / APP
    exe = folder / (APP + (".exe" if os.name == "nt" else ""))
    if not exe.exists():
        print(f"build finished but {exe} is missing")
        return 1
    report = BUILD / "selfcheck.txt"
    report.unlink(missing_ok=True)
    home = BUILD / "selfcheck-home"  # never the real %LOCALAPPDATA% folder
    print("> checking the packaged app", flush=True)
    result = subprocess.run([str(exe), "--role", "selfcheck", str(report)],
                            env=dict(os.environ, EGX_HOME=str(home)), timeout=300, check=False)
    print(report.read_text(encoding="utf-8") if report.exists() else "no self-check report")
    if result.returncode != 0:
        print("The packaged app failed its self-check; see above.")
        return 1
    outputs = [exe]
    if not args.no_zip:
        suffix = "win64" if os.name == "nt" else sys.platform
        archive = DIST / f"EGX-Robo-Advisor-{ver}-{suffix}.zip"
        zip_folder(folder, archive)
        outputs.append(archive)
    if os.name == "nt" and not args.no_installer:
        iscc = find_iscc()
        if iscc:
            run([iscc, f"/DAppVersion={ver}", f"/DSourceDir={folder}", f"/DOutputDir={DIST}",
                 f"/DIconFile={icon}", str(ROOT / "packaging" / "installer.iss")])
            outputs.append(DIST / f"EGX-Robo-Advisor-Setup-{ver}.exe")
        else:
            print("\nInno Setup 6 not found, so no installer was made. To get one:\n"
                  "  winget install JRSoftware.InnoSetup\nthen run build_exe.cmd again.")

    print("\nDone:")
    for path in outputs:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

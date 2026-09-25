@echo off
rem Builds EGX Robo-Advisor.exe (and a zip, and an installer if Inno Setup 6 is
rem installed) into the dist folder. Run install.cmd once before this.
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo The project environment is missing. Run install.cmd first.
  pause
  exit /b 1
)
echo Installing the build tools ...
"%PY%" -m pip install --quiet --upgrade "pyinstaller>=6.10" Pillow || goto :fail
"%PY%" -m pip install --quiet -e ".[desktop,dashboard,marketdata]" -c packaging\constraints.txt || goto :fail
"%PY%" packaging\build.py %* || goto :fail
echo.
echo The app is in: %~dp0dist\EGX Robo-Advisor\EGX Robo-Advisor.exe
explorer "%~dp0dist"
pause
exit /b 0
:fail
echo.
echo The build failed. The messages above say where.
pause
exit /b 1

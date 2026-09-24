@echo off
rem One-time setup for the EGX robo-advisor. Double-click this file.
setlocal
cd /d "%~dp0"
set PYVER=
py -3.13 -c "" >nul 2>&1 && set PYVER=3.13
if not defined PYVER py -3.12 -c "" >nul 2>&1 && set PYVER=3.12
if not defined PYVER (
  echo Python 3.13 is not installed. Install it with:
  echo     winget install Python.Python.3.13
  echo then double-click install.cmd again.
  pause
  exit /b 1
)
py -%PYVER% install.py %*
pause

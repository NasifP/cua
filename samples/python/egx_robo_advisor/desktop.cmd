@echo off
rem The EGX robo-advisor desktop app: dashboard, chat and Thndr X in one window.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The bot is not installed yet. Double-click install.cmd first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" desktop.py

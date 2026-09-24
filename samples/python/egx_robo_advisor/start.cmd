@echo off
rem Starts the EGX robo-advisor: computer server, dashboard and agent together.
rem Double-click this file. Close with Ctrl+C, which halts the bot first.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The bot is not installed yet. Double-click install.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" launch.py %*
pause

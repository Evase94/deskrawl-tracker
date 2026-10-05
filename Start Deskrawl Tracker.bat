@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" deskrawl_tracker.py
) else (
  start "" pythonw deskrawl_tracker.py
)

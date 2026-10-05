@echo off
rem Creates a private Python environment (.venv) and installs the packages the tracker needs.
cd /d "%~dp0"
echo Creating Python environment ...
py -3.12 -m venv .venv 2>nul || python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Python 3.12 was not found. Please install it: https://www.python.org/downloads/
  echo Tick "Add python.exe to PATH" during installation.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Installation failed.
  pause
  exit /b 1
)
echo.
echo Done. Start with "Start Deskrawl Tracker.bat".
pause

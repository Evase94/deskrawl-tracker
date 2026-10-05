@echo off
rem Creates a private Python environment (.venv) and installs the packages the tracker needs.
cd /d "%~dp0"
echo Lege Python-Umgebung an ...
py -3.12 -m venv .venv 2>nul || python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Python 3.12 wurde nicht gefunden. Bitte installieren: https://www.python.org/downloads/
  echo Beim Installieren "Add python.exe to PATH" anhaken.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Installation fehlgeschlagen.
  pause
  exit /b 1
)
echo.
echo Fertig. Starten mit "Deskrawl Tracker starten.bat".
pause

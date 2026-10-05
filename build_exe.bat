@echo off
rem Builds dist\DeskrawlTracker\DeskrawlTracker.exe (folder build) and dist\DeskrawlTracker.zip for a GitHub release.
cd /d "%~dp0"
set PY=python
set PYTHONPATH=%~dp0build_helper
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
%PY% -m pip install pyinstaller
%PY% -m PyInstaller --noconfirm --clean --windowed --name DeskrawlTracker ^
  --add-data "data;data" --add-data "legendaries.json;." ^
  --collect-data rapidocr_onnxruntime --hidden-import winrt.windows.media.ocr --hidden-import winrt.windows.globalization --hidden-import winrt.windows.storage.streams --hidden-import winrt.windows.graphics.imaging --hidden-import winrt.windows.foundation --hidden-import winrt.windows.foundation.collections ^
  deskrawl_tracker.py
if errorlevel 1 (pause & exit /b 1)
copy /y "README.md" "dist\DeskrawlTracker\" >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path dist\DeskrawlTracker -DestinationPath dist\DeskrawlTracker.zip"
echo Fertig: dist\DeskrawlTracker.zip

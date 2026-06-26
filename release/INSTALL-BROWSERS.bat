@echo off
REM Re-download Playwright Chromium if playwright-browsers folder is missing.
cd /d "%~dp0"
set PLAYWRIGHT_BROWSERS_PATH=%~dp0playwright-browsers
if not exist "%PLAYWRIGHT_BROWSERS_PATH%" mkdir "%PLAYWRIGHT_BROWSERS_PATH%"
echo Installing Chromium to %PLAYWRIGHT_BROWSERS_PATH%
echo This is ~500MB and needs internet.
BOL-BOT.exe --help >nul 2>&1
python -m playwright install chromium
if errorlevel 1 (
  echo Failed. Install Python + playwright, or re-download the full release zip.
  pause
  exit /b 1
)
echo Done. playwright-browsers folder is ready.
pause

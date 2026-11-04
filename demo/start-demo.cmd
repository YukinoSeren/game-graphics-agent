@echo off
setlocal
set "DEMO_DIR=%~dp0"

set "PY_CMD="
py -3.13 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3.13"
if not defined PY_CMD (
  py -3 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY_CMD=py -3"
)
if not defined PY_CMD (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD (
  echo [demo] Python 3.11+ was not found. Install Python and run again.
  pause
  exit /b 1
)

echo [demo] Starting the local demo server (%PY_CMD%) ...
echo [demo] Demo data is written to demountime. Press Ctrl+C to stop.
%PY_CMD% -B "%DEMO_DIR%server.py" %*
set "DEMO_EXIT=%ERRORLEVEL%"
if not "%DEMO_EXIT%"=="0" pause
exit /b %DEMO_EXIT%

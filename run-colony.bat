@echo off
REM ===========================================================================
REM  Double-click this to launch the ant-colony WEB OBSERVER and open it in your
REM  default browser. Bots do NOT start automatically any more — click "Start" in
REM  the browser (choose how many scouts / wanderers) to launch them on demand.
REM
REM  Requirements: the Canary docker server must be running, and Python 3.12
REM  installed.
REM
REM  --pool N   : how many accounts (test1..testN) the browser can start bots from.
REM  --record   : flight (default, black box) | full (record every session) | none
REM
REM  Close this window (or press Ctrl+C) to stop the observer and all bots.
REM ===========================================================================

cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting the ant colony observer... a browser window will open shortly.
echo Click "Start" in the page to launch bots. (Close this window to stop.)
echo.

REM --open-browser opens the dashboard once the server is bound (no race). The
REM inputs default to --scouts / --bots; the pool caps how many you can launch.
"%PY%" -m antbot farm --password test --pool 6 --scouts 3 --bots 0 --open-browser

echo.
echo Colony observer stopped.
pause

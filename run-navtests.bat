@echo off
REM ===========================================================================
REM  Run the antbot navigation test suite.
REM    Double-click              -> runs EVERY test
REM    run-navtests.bat door     -> only tests whose name contains "door"
REM
REM  Requires Docker with the otbr-* containers available (the harness starts the
REM  DB / game server as needed). Most tests use the GM fast path and take a few
REM  seconds; any reset_map test restarts the game server (~1 min each).
REM
REM  Exits non-zero unless every test passes, so it doubles as a CI gate.
REM ===========================================================================
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -m antbot.navtests %*
set "CODE=%ERRORLEVEL%"

echo.
echo (exit code %CODE%)
pause
exit /b %CODE%

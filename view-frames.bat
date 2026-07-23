@echo off
REM ===========================================================================
REM  Open the frame-by-frame pathfinding visualizer.
REM    Double-click                        -> opens the empty viewer (drag a file in)
REM    view-frames.bat frames.jsonl        -> opens it pre-loaded with that file
REM
REM  Generate a frames file first with:
REM    run-navtests.bat "test name" --frame-log frames.jsonl
REM
REM  Opens your default browser; the server keeps running in this window until
REM  you close it or press Ctrl+C.
REM ===========================================================================
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -m antbot.frame_viewer %*

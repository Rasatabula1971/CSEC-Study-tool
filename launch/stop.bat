@echo off
REM ============================================================
REM  CSEC AI Study Partner - stop the study system
REM  Finds the FastAPI (uvicorn) server listening on port 8000
REM  and stops it, so the SSD can be removed safely. Ollama is
REM  left running; closing it is optional.
REM ============================================================

echo Stopping the CSEC AI Study Partner...
echo.

set "KILLED="
REM Find the PID listening on port 8000 (more precise than killing all python).
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do (
    echo Stopping server process %%p ...
    taskkill /f /pid %%p >nul 2>&1
    set "KILLED=1"
)

if not defined KILLED (
    echo No study server was running on port 8000.
    echo If you still cannot remove the SSD, close any open command windows.
) else (
    echo Server stopped.
)

echo.
echo Safe to remove the SSD.
echo.
pause

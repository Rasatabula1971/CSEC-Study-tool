@echo off
REM ============================================================
REM  CSEC AI Study Partner - launcher
REM  Run this from the repo root. Set SSD_ROOT in .env first.
REM
REM  Gating policy (do not change):
REM   - Ollama reachability is a HARD gate. The curl on /api/tags
REM     (== ollama_client.ollama_health()) keeps its `exit /b 1`.
REM     Ollama down is a real blocker and must stop startup.
REM   - ram_check.py is ADVISORY ONLY: it always exits 0 and must
REM     NOT gate startup. The real RAM test is whether a session
REM     runs without freezing, not a snapshot at launch.
REM ============================================================

echo Checking SSD...
if not exist "%SSD_ROOT%" (
    echo ERROR: SSD not mounted at %SSD_ROOT%. Plug in the drive and retry.
    pause
    exit /b 1
)
echo Starting Ollama...
start "" ollama serve
timeout /t 3 /nobreak >nul
curl -s http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo ERROR: Ollama did not start. Check Ollama installation.
    pause
    exit /b 1
)
echo Starting FastAPI...
cd /d "%~dp0.."
REM --reload: pick up code/route changes without a manual restart. Prevents the
REM "endpoint added on disk but the running process never registered it" gotcha
REM (e.g. GET /api/objective returning 404 from a stale process).
start "" python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload
timeout /t 2 /nobreak >nul
curl -s http://127.0.0.1:8000/health >nul 2>&1
if errorlevel 1 (
    echo ERROR: FastAPI did not start. Check the terminal for errors.
    pause
    exit /b 1
)
echo Study system ready. Opening browser...
start http://127.0.0.1:8000

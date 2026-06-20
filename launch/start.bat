@echo off
REM ============================================================
REM  CSEC AI Study Partner - launcher
REM  Run this from the repo root (double-click start.bat).
REM  Set SSD_ROOT in .env first.
REM
REM  How this window behaves:
REM   - This window runs the study server in the FOREGROUND. It
REM     stays open and "busy" (filling with server log lines) for
REM     the whole study session. That open window IS the running
REM     indicator. To stop studying, CLOSE THIS WINDOW -- that
REM     shuts the server down cleanly. There is no orphaned
REM     background process and no separate stop step.
REM   - Ollama is started in the background on purpose and is LEFT
REM     running when this window closes (it uses little memory when
REM     idle and is shared by every session). Only the FastAPI
REM     server is tied to this window.
REM
REM  Gating policy (do not change):
REM   - Ollama reachability is a HARD gate. The curl on /api/tags
REM     (== ollama_client.ollama_health()) keeps its `exit /b 1`.
REM     Ollama down is a real blocker and must stop startup.
REM
REM  Note for developers: this launcher intentionally runs uvicorn
REM  with NO --reload. Reload spawns a watcher child that can
REM  outlive the parent and orphan the server -- the exact problem
REM  this foreground launcher fixes. If you are doing active backend
REM  work, run uvicorn with --reload yourself from a separate dev
REM  shell -- do not add it back here.
REM ============================================================

REM Load SSD_ROOT from .env when it is not already in the environment, so a plain
REM double-click of the desktop shortcut works without any system env var set.
REM .env stays the single source of truth for the drive letter (CLAUDE.md SSD rules).
if "%SSD_ROOT%"=="" (
    if exist "%~dp0..\.env" (
        for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%~dp0..\.env") do (
            if /i "%%a"=="SSD_ROOT" set "SSD_ROOT=%%b"
        )
    )
)

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

REM Open the browser BEFORE starting the server: the server runs in
REM the foreground below and never returns, so anything that must
REM happen "after launch" has to happen here first. The page may be
REM blank for a few seconds until the server finishes starting --
REM that is expected.
REM Open the study app in its OWN dedicated Chrome window (--app):
REM a single borderless window with NO session-restore and no other
REM tabs. Plain `start http://...` cold-starts the default browser,
REM and Chrome set to "Continue where you left off" then restores the
REM previous session AND opens this URL -- which surfaced as TWO
REM windows on launch. The app window avoids that entirely. If Chrome
REM is not installed, fall back to the default browser so launch still
REM works (it just loses the single-window guarantee).
echo Opening browser...
set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if defined CHROME (
    start "" "%CHROME%" --app=http://127.0.0.1:8000
) else (
    start http://127.0.0.1:8000
)

echo.
echo ============================================================
echo   CSEC Study Partner is starting up.
echo.
echo   This takes about 20 seconds the first time, while the
echo   AI model loads into memory. The browser page will look
echo   blank or show an error until it finishes -- that is
echo   normal. Just wait, then refresh the page once.
echo.
echo   To stop studying, just close this window.
echo ============================================================
echo.
echo   Loading... (startup messages will appear below)
echo.

REM Run the server in the FOREGROUND (no `start`, no --reload). This
REM call blocks: the window stays open and busy for the whole
REM session, and closing the window stops the server cleanly.
cd /d "%~dp0.."
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000

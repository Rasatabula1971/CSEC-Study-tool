@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0\.."
set "SSD_ROOT=%CD%"

REM Derive every path from SSD_ROOT — never hardcode a drive letter
set "OLLAMA_MODELS=%SSD_ROOT%\01_MODELS\Ollama"
set "OLLAMA_HOST=127.0.0.1:11434"
set "PYTHONPATH=%SSD_ROOT%\01_TOOLS\lib"
set "PYTHON=%SSD_ROOT%\01_TOOLS\Python\python.exe"
set "OLLAMA=%SSD_ROOT%\01_TOOLS\Ollama\ollama.exe"
set "DB_PATH=%SSD_ROOT%\02_DATABASE\csec.sqlite"

REM Backup today's DB before starting (skip if today's backup already exists)
for /f "tokens=2 delims==" %%D in ('wmic os get LocalDateTime /value') do set DT=%%D
set "TODAY=%DT:~0,8%"
set "BACKUP=%SSD_ROOT%\07_BACKUPS\csec_backup_%TODAY%.sqlite"
if not exist "%BACKUP%" (
    echo Backing up database...
    copy /y "%DB_PATH%" "%BACKUP%" >nul 2>&1
)

REM 1. Start Ollama if not already running
curl -s http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo Starting Ollama...
    start "Ollama" /min "%OLLAMA%" serve
    REM Poll for readiness instead of fixed sleep
    set /a tries=0
    :wait_ollama
    timeout /t 1 /nobreak >nul
    curl -s http://127.0.0.1:11434/api/tags >nul 2>&1
    if errorlevel 1 (
        set /a tries+=1
        if !tries! lss 15 goto wait_ollama
        echo ERROR: Ollama did not start within 15 seconds.
        pause
        exit /b 1
    )
)

REM 2. Start FastAPI (app is at 06_BACKEND\backend\app.py on the SSD)
echo Starting study system...
start "CSEC Study" /min "%PYTHON%" -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --app-dir "%SSD_ROOT%\06_BACKEND"

REM 3. Wait for FastAPI to be ready (polls /health — returns 200 once FastAPI is up)
set /a tries=0
:wait_api
timeout /t 1 /nobreak >nul
curl -s http://127.0.0.1:8000/health >nul 2>&1
if errorlevel 1 (
    set /a tries+=1
    if !tries! lss 20 goto wait_api
    echo ERROR: Study system did not start within 20 seconds.
    pause
    exit /b 1
)

REM 4. Open the browser
start "" http://127.0.0.1:8000

echo.
echo Study system is running. Close this window when you're done studying.
echo (Or run 00_LAUNCH\shutdown.bat to stop it cleanly.)
pause

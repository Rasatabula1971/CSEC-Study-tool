@echo off
REM ============================================================
REM  CSEC AI Study Partner - First-Time Setup
REM  Double-click this once on a new laptop. It installs the
REM  Python packages, downloads the AI models onto the SSD, and
REM  builds the database. Run launch\start.bat afterwards.
REM ============================================================
setlocal
cd /d "%~dp0.."

echo ============================================================
echo   CSEC AI Study Partner - First-Time Setup
echo ============================================================
echo.

REM ---- [1/8] Python -----------------------------------------
echo [1/8] Checking for Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo Python not found. Install Python 3.11+ from https://python.org/downloads
    echo IMPORTANT: Check 'Add Python to PATH' during installation.
    echo.
    pause
    exit /b 1
)
python --version
echo.

REM ---- [2/8] pip --------------------------------------------
echo [2/8] Checking for pip...
pip --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo pip not found. It normally comes with Python 3.11+.
    echo Reinstall Python from https://python.org/downloads
    echo IMPORTANT: Check 'Add Python to PATH' during installation.
    echo.
    pause
    exit /b 1
)
echo.

REM ---- [3/8] Configuration (.env) ---------------------------
echo [3/8] Checking configuration ^(.env^)...
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Created .env from the template. It assumes the SSD is drive D:.
    echo If your SSD uses a different letter ^(see Step 1 in SETUP.md^), open
    echo .env in Notepad, change the drive letter in every path, save it, and
    echo run this setup again.
    echo.
    pause
    exit /b 1
)

REM Read SSD_ROOT from .env so the model path is never hardcoded to D:.
set "SSD_ROOT="
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="SSD_ROOT" set "SSD_ROOT=%%b"
)
if not defined SSD_ROOT (
    echo ERROR: SSD_ROOT is not set in .env. Open .env in Notepad and set it.
    pause
    exit /b 1
)
echo Using SSD root: %SSD_ROOT%
echo.

REM ---- [4/8] Python dependencies ----------------------------
echo [4/8] Installing Python dependencies (this can take a few minutes)...
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Some dependencies failed to install. Read the messages above.
    echo Check your internet connection, make sure Python 3.11+ is installed,
    echo then run this setup again.
    echo.
    pause
    exit /b 1
)
echo.

REM ---- [5/8] Ollama -----------------------------------------
echo [5/8] Checking for Ollama...
ollama --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo Ollama not found. Install from https://ollama.com/download
    echo Run this setup script again after installing.
    echo.
    pause
    exit /b 1
)
echo.

REM ---- [6/8] Ollama model storage on the SSD ----------------
echo [6/8] Checking Ollama model storage location...
if not defined OLLAMA_MODELS (
    echo.
    echo IMPORTANT: Set Ollama model storage to the SSD. Run this in PowerShell:
    echo.
    echo     setx OLLAMA_MODELS "%SSD_ROOT%\01_MODELS\Ollama"
    echo.
    echo Then close all windows, restart Ollama, and run this setup again.
    echo.
    pause
    exit /b 1
)
echo Models will be stored at: %OLLAMA_MODELS%
echo.

REM ---- [7/8] Download the AI models -------------------------
echo [7/8] Downloading AI models (this takes 10-20 minutes the first time)...
ollama pull llama3.2:3b
if errorlevel 1 (
    echo.
    echo ERROR: Failed to download llama3.2:3b. Check your internet and try again.
    echo.
    pause
    exit /b 1
)
ollama pull nomic-embed-text
if errorlevel 1 (
    echo.
    echo ERROR: Failed to download nomic-embed-text. Check your internet and retry.
    echo.
    pause
    exit /b 1
)
echo.

REM ---- [8/8] Database + verification -------------------------
echo [8/8] Building the database and verifying the install...
python backend/db/init_db.py
if errorlevel 1 (
    echo.
    echo ERROR: Database setup failed. Make sure the SSD is plugged in and the
    echo drive letter in .env matches the SSD, then run this setup again.
    echo.
    pause
    exit /b 1
)
python -m pytest tests/test_schema.py -v
if errorlevel 1 (
    echo.
    echo ERROR: Verification tests failed. Read the messages above and try the
    echo setup again. If it keeps failing, ask for help.
    echo.
    pause
    exit /b 1
)
echo.

echo ============================================================
echo   Setup complete! Run launch\start.bat to start studying.
echo ============================================================
echo.
pause

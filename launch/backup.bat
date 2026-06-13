@echo off
REM backup.bat
REM Copies csec.sqlite to 07_BACKUPS\csec_backup_{date}.sqlite
REM Run from the repo root: launch\backup.bat
REM Requires SSD_ROOT to be set in .env OR as an environment variable.

setlocal

REM --- Read SSD_ROOT from .env if it exists ---
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if "%%A"=="SSD_ROOT" set SSD_ROOT=%%B
    )
)

if "%SSD_ROOT%"=="" (
    echo ERROR: SSD_ROOT is not set. Check your .env file.
    pause
    exit /b 1
)

if not exist "%SSD_ROOT%" (
    echo ERROR: SSD not found at %SSD_ROOT%
    echo Plug in the external SSD and try again.
    pause
    exit /b 1
)

set DB_SOURCE=%SSD_ROOT%\02_DATABASE\csec.sqlite
set BACKUP_DIR=%SSD_ROOT%\07_BACKUPS

if not exist "%DB_SOURCE%" (
    echo ERROR: Database not found at %DB_SOURCE%
    echo Run python backend\db\init_db.py first.
    pause
    exit /b 1
)

if not exist "%BACKUP_DIR%" (
    mkdir "%BACKUP_DIR%"
)

REM Build date string YYYYMMDD
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set dt=%%I
set DATESTAMP=%dt:~0,8%

set BACKUP_FILE=%BACKUP_DIR%\csec_backup_%DATESTAMP%.sqlite

echo Backing up database...
echo   Source : %DB_SOURCE%
echo   Dest   : %BACKUP_FILE%

xcopy /Y "%DB_SOURCE%" "%BACKUP_FILE%*" >nul

if errorlevel 1 (
    echo ERROR: Backup failed.
    pause
    exit /b 1
)

echo.
echo Backup complete: %BACKUP_FILE%
endlocal

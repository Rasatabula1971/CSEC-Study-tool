@echo off
setlocal
cd /d "%~dp0\.."
set "SSD_ROOT=%CD%"

REM 1. Show welcome (opens in default browser)
start "" "%SSD_ROOT%\00_LAUNCH\welcome.html"
timeout /t 4 /nobreak >nul

REM 2. Sanity check: required folders exist
for %%D in (01_TOOLS\Python 01_TOOLS\Ollama 01_MODELS\Ollama 02_DATABASE 06_BACKEND) do (
    if not exist "%SSD_ROOT%\%%D" (
        echo ERROR: Missing %%D on the SSD. Contact Ricky.
        pause
        exit /b 1
    )
)

REM 3. Free disk space check on the SSD (need ~2 GB for runtime working space)
for /f "tokens=3" %%S in ('dir /-c "%SSD_ROOT%\" ^| findstr /C:"bytes free"') do set FREE=%%S
echo Free space on SSD: %FREE% bytes

REM 4. Create desktop shortcut (per-user, no admin needed)
powershell -NoProfile -Command ^
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(\"$env:USERPROFILE\Desktop\CSEC Study.lnk\"); ^
     $s.TargetPath = '%SSD_ROOT%\START_STUDYING.bat'; ^
     $s.WorkingDirectory = '%SSD_ROOT%'; ^
     $s.IconLocation = '%SSD_ROOT%\01_TOOLS\Python\python.exe'; ^
     $s.Save()"

REM 5. Mark setup complete BEFORE launching, so a crash mid-launch doesn't re-run setup
echo %DATE% %TIME% > "%SSD_ROOT%\.setup_complete"

REM 6. Hand off to the normal launcher
call "%SSD_ROOT%\00_LAUNCH\launch.bat"

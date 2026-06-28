@echo off
REM Single entry point. Routes to first_run.bat or launch.bat.
cd /d "%~dp0"
if not exist ".setup_complete" (
    call "00_LAUNCH\first_run.bat"
) else (
    call "00_LAUNCH\launch.bat"
)

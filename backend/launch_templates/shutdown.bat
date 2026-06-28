@echo off
echo Stopping study system...
taskkill /F /FI "WINDOWTITLE eq CSEC Study*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Ollama*" >nul 2>&1
taskkill /F /IM ollama.exe >nul 2>&1
echo Done. Safe to unplug the SSD.
timeout /t 3 >nul

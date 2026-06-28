# CSEC AI Study Partner — Portable SSD Bootstrap Plan

**Goal:** Rylee plugs the SSD into her laptop, double-clicks one file, and is studying within 60 seconds. No internet required. No Python or Ollama install on her machine. No UAC prompts. Survives the SSD being mounted on any drive letter (D:, E:, F:, etc.).

This document is the build plan for the bootstrap layer. The study app itself (Stages 1–8 of the original playbook) is unchanged.

---

## 1. Architecture Decision: Pre-Bundle, Don't Download

Everything Rylee needs is on the SSD before it leaves your hands. Nothing is downloaded at first run.

**Why not download-at-runtime:**
- Multi-GB Ollama models on a flaky connection = a half-installed system she can't recover from
- The PDR explicitly requires offline operation (VAL-01)
- Failure modes during install are exactly the ones she can't diagnose
- One hour of prep on your end vs. an unrecoverable failure on hers — not a close call

**Cost:** ~500 MB extra for the Python runtime and pip wheels, on top of what's already going on the SSD (models, DB, documents).

---

## 2. SSD Layout (additions to existing structure)

Additions in **bold**. Everything else is from CLAUDE.md.

```
{SSD_ROOT}\
├── START_STUDYING.bat              ← THE ONLY FILE Rylee touches  ★
├── .setup_complete                 ← marker, created after first run
│
├── 00_LAUNCH\
│   ├── first_run.bat               ← welcome + DB init + shortcut creation
│   ├── launch.bat                  ← subsequent runs (Ollama + FastAPI + browser)
│   ├── shutdown.bat                ← clean stop (kills both processes)
│   └── welcome.html                ← opened on first run, explains what's happening
│
├── 01_TOOLS\                       ★ NEW
│   ├── Python\                     ← Python 3.11 embeddable distribution
│   │   ├── python.exe
│   │   ├── python311._pth          ← modified to include ..\..\lib
│   │   └── ...
│   ├── lib\                        ← pre-installed dependencies (--target install)
│   │   ├── fastapi\
│   │   ├── uvicorn\
│   │   ├── sqlite_vec\
│   │   └── ...
│   ├── wheels\                     ← raw .whl files (recovery if lib\ corrupted)
│   └── Ollama\
│       ├── ollama.exe              ← portable binary, no install
│       └── lib\                    ← Ollama runtime DLLs
│
├── 01_MODELS\
│   └── Ollama\                     ← pre-pulled models (llama3.2:3b, nomic-embed-text)
│       ├── blobs\
│       └── manifests\
│
├── 02_DATABASE\
│   └── csec.sqlite                 ← pre-initialized with all 7 subjects locked
│
├── 03_KNOWLEDGE_BASE\              ← (as per CLAUDE.md)
├── 04_REPORTS\
├── 05_PROMPTS\
├── 06_BACKEND\                     ← the FastAPI app (committed from repo)
└── 07_BACKUPS\
```

★ = files Rylee can see and might click. Everything else is plumbing.

---

## 3. The Three Launchers

Three batch files, each with one job. Keep them small and obvious.

### 3.1 `START_STUDYING.bat` (SSD root — what Rylee clicks)

```batch
@echo off
REM Single entry point. Routes to first_run.bat or launch.bat.
cd /d "%~dp0"
if not exist ".setup_complete" (
    call "00_LAUNCH\first_run.bat"
) else (
    call "00_LAUNCH\launch.bat"
)
```

That's it. The whole file. Five lines.

### 3.2 `00_LAUNCH\first_run.bat`

Welcome → environment setup → DB sanity check → create desktop shortcut → handoff to `launch.bat` → write marker.

```batch
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
```

### 3.3 `00_LAUNCH\launch.bat`

This runs every time. Idempotent. Starts Ollama → starts FastAPI → opens browser.

```batch
@echo off
setlocal
cd /d "%~dp0\.."
set "SSD_ROOT=%CD%"

REM Derive every path from SSD_ROOT — never hardcode a drive letter
set "OLLAMA_MODELS=%SSD_ROOT%\01_MODELS\Ollama"
set "OLLAMA_HOST=127.0.0.1:11434"
set "PYTHONPATH=%SSD_ROOT%\01_TOOLS\lib"
set "PYTHON=%SSD_ROOT%\01_TOOLS\Python\python.exe"
set "OLLAMA=%SSD_ROOT%\01_TOOLS\Ollama\ollama.exe"
set "DB_PATH=%SSD_ROOT%\02_DATABASE\csec.sqlite"
set "SSD_ROOT=%SSD_ROOT%"

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

REM 2. Start FastAPI
echo Starting study system...
start "CSEC Study" /min "%PYTHON%" -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --app-dir "%SSD_ROOT%\06_BACKEND"

REM 3. Wait for FastAPI to be ready
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
```

### 3.4 `00_LAUNCH\shutdown.bat`

```batch
@echo off
echo Stopping study system...
taskkill /F /FI "WINDOWTITLE eq CSEC Study*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Ollama*" >nul 2>&1
taskkill /F /IM ollama.exe >nul 2>&1
echo Done. Safe to unplug the SSD.
timeout /t 3 >nul
```

---

## 4. What You (Ricky) Do Once, on Your Machine

This is the prep work. Do it on your dev laptop, then copy the SSD to Rylee's.

### 4.1 Bundle the portable Python runtime

```powershell
# Download the embeddable zip (no installer, just unzip)
$url = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
Invoke-WebRequest $url -OutFile "$env:TEMP\py311.zip"
Expand-Archive "$env:TEMP\py311.zip" -DestinationPath "E:\CSEC_AI_STUDY_PARTNER\01_TOOLS\Python"

# CRITICAL: edit python311._pth to enable site-packages and PYTHONPATH
# By default the embeddable distro has 'import site' commented out.
# The file should contain:
#   python311.zip
#   .
#   ..\lib
#   import site
```

Then unblock the embeddable Python from running:
```powershell
Get-ChildItem "E:\CSEC_AI_STUDY_PARTNER\01_TOOLS\Python" -Recurse | Unblock-File
```

### 4.2 Pre-install all Python dependencies to the SSD

```powershell
$SSD = "E:\CSEC_AI_STUDY_PARTNER"
$PY = "$SSD\01_TOOLS\Python\python.exe"

# First, install pip into the embeddable distribution (it doesn't ship with pip)
Invoke-WebRequest "https://bootstrap.pypa.io/get-pip.py" -OutFile "$env:TEMP\get-pip.py"
& $PY "$env:TEMP\get-pip.py" --target "$SSD\01_TOOLS\lib"

# Download wheels for offline recovery
& $PY -m pip download -r "C:\CSEC-study-partner\requirements.txt" -d "$SSD\01_TOOLS\wheels"

# Install dependencies to the portable lib folder
& $PY -m pip install -r "C:\CSEC-study-partner\requirements.txt" --target "$SSD\01_TOOLS\lib" --find-links "$SSD\01_TOOLS\wheels"
```

### 4.3 Bundle the portable Ollama binary

```powershell
# Ollama ships a standard Windows installer, but the EXE inside it runs standalone.
# Install Ollama on your machine first, then copy the binaries:
Copy-Item "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" "E:\CSEC_AI_STUDY_PARTNER\01_TOOLS\Ollama\"
Copy-Item "$env:LOCALAPPDATA\Programs\Ollama\lib\*" "E:\CSEC_AI_STUDY_PARTNER\01_TOOLS\Ollama\lib\" -Recurse
```

Verify it runs portably:
```powershell
$env:OLLAMA_MODELS = "E:\CSEC_AI_STUDY_PARTNER\01_MODELS\Ollama"
& "E:\CSEC_AI_STUDY_PARTNER\01_TOOLS\Ollama\ollama.exe" list
# Should show llama3.2:3b and nomic-embed-text without prompting for install
```

### 4.4 Pre-pull the models onto the SSD

```powershell
$env:OLLAMA_MODELS = "E:\CSEC_AI_STUDY_PARTNER\01_MODELS\Ollama"
ollama pull llama3.2:3b
ollama pull nomic-embed-text
```

(This step you've already done as part of Stage 3.)

### 4.5 Pre-build the database

Run all of Stages 1, 2, 4, 8 from the existing playbook against the SSD before shipping. The database that arrives on Rylee's laptop should already have:
- All 7 subjects locked
- All ingested chunks and mark points
- A clean `weakness_log` (empty — she starts fresh)

Confirm:
```powershell
& "E:\CSEC_AI_STUDY_PARTNER\01_TOOLS\Python\python.exe" -c "import sqlite3; db = sqlite3.connect('E:/CSEC_AI_STUDY_PARTNER/02_DATABASE/csec.sqlite'); print(db.execute('SELECT subject_id, syllabus_locked FROM subjects').fetchall())"
```

All 7 should return `syllabus_locked = 1`.

---

## 5. The `welcome.html` File

A single static HTML page, no JS required. Renders in any browser. Make it friendly, not technical.

Contents (sketch — write the actual copy in your voice):

> **Hi Rylee — welcome to your study system.**
>
> This SSD has everything you need: lessons, past papers, mark schemes, and the AI tutor. The first time you open it takes about 30 seconds to get ready. After that it's instant.
>
> Two things:
>
> 1. Always close the system with the **Stop Studying** shortcut before you unplug the SSD. The browser tab won't be enough.
> 2. If anything goes wrong, message Dad. Don't try to fix it yourself — there's nothing you can break by waiting.
>
> Closing this page in 5 seconds, then your study screen will open.

---

## 6. What Needs Admin Rights

**Nothing.** The whole flow runs in user space.

- Embeddable Python: just files, no install
- Ollama: portable binary, no service install
- Desktop shortcut: created under `%USERPROFILE%\Desktop` (per-user)
- Ports 8000 and 11434: localhost-only, no firewall rule needed
- `setx` is never used (those need admin and persist machine-wide); environment is set per-session with `set`

The only thing Windows might prompt about is **SmartScreen** when she double-clicks `START_STUDYING.bat` for the first time. There are two ways to handle this:

**Option A (recommended): Unblock files on your end before shipping.**
```powershell
Get-ChildItem "E:\CSEC_AI_STUDY_PARTNER" -Recurse | Unblock-File
```
Files copied from the SSD won't have the "downloaded from internet" zone tag, so SmartScreen leaves them alone.

**Option B:** Tell Rylee in advance: "if it says 'Windows protected your PC', click **More info** → **Run anyway**." One time only, then never again.

---

## 7. Failure Modes & Recovery

The system she'll use it on isn't yours. Things will go sideways. Plan for these:

| Failure | What she sees | Recovery |
|---|---|---|
| SSD mounted as different letter | Nothing — `%~dp0` handles it automatically | None needed |
| Antivirus quarantines `ollama.exe` | `ERROR: Ollama did not start within 15 seconds` | She messages you; you talk her through adding an exclusion, or ship a second SSD |
| Port 8000 already in use (rare on her laptop) | `ERROR: Study system did not start` | Edit `launch.bat` to use 8001 |
| Browser doesn't auto-open | She sees the terminal but no browser | Shortcut in `welcome.html`: "If your browser didn't open, go to http://127.0.0.1:8000" |
| She unplugs without shutdown | DB might be left in WAL state | Next launch auto-recovers (SQLite handles this); add `PRAGMA journal_mode=WAL` is already standard |
| `.setup_complete` deleted accidentally | First-run dialog appears again on next launch | Harmless — first_run.bat is idempotent |

Add one more safety net: on every launch, `launch.bat` does a one-line backup of the DB to `07_BACKUPS\` before starting FastAPI. Cheap insurance.

---

## 8. Build Tasks (for Claude Code)

Ordered list. Hand these to Claude Code one at a time, each with a `/clear` between.

1. **Bootstrap scripts** — write the four `.bat` files (Section 3) and `welcome.html`. Place them under `06_BACKEND\launch_templates\` in the repo. They get copied to `00_LAUNCH\` on the SSD by a build script.

2. **Build script** — `tools/build_ssd.ps1` that takes a target drive letter and:
   - Validates the embeddable Python is in place
   - Runs the dependency bundling (Section 4.2)
   - Copies Ollama binaries (4.3)
   - Copies launcher files to `00_LAUNCH\`
   - Copies the latest `06_BACKEND\` from the repo
   - Runs `Unblock-File` over the entire SSD
   - Prints a checklist of every step's pass/fail

3. **Health endpoint hardening** — verify `backend/app.py`'s `/health` returns 200 even before Ollama is fully warm, so `launch.bat`'s polling loop doesn't time out on cold start. The check is "FastAPI is up," not "the whole system is healthy."

4. **Drive-letter-agnostic config loading** — audit every place `.env` is read. Replace any hardcoded `D:\` or `E:\` with `os.path.dirname(os.path.abspath(__file__))`-derived paths or `SSD_ROOT` from the environment. Add a startup assertion that `SSD_ROOT` is set and exists; fail fast with a clear message if not.

5. **Backup-on-launch** — add a five-line block at the top of `launch.bat` that copies `csec.sqlite` to `07_BACKUPS\csec_backup_{date}.sqlite`, skipping if today's backup already exists.

6. **Clean-shutdown handling** — `backend/app.py` lifespan handler should close the DB connection on shutdown (SIGTERM from `taskkill`). Without this, the WAL file can be left dirty.

7. **Smoke test on a clean VM** — spin up a fresh Windows 11 VM with no Python, no Ollama, nothing. Plug in (or mount) the SSD. Double-click `START_STUDYING.bat`. The acceptance criteria:
   - First run completes in under 60 seconds
   - No UAC prompts
   - Browser opens to a working study screen
   - She can ask for a POB lesson and get a response
   - Shutdown via `shutdown.bat` leaves no orphaned processes (verify with Task Manager)
   - Second launch is under 10 seconds

---

## 9. The Build Order (Sequencing)

Don't build the bootstrap layer until the app itself is stable. The order is:

1. **First**: finish the app (Stages 1–8 of `build_playbook.md`). Bootstrap is meaningless if the app underneath isn't ready.
2. **Then**: write the bootstrap scripts (Section 3) and the build script (Section 8, task 2).
3. **Then**: do one full SSD build on your end, against your own laptop with the SSD mounted.
4. **Then**: test on the clean VM (Section 8, task 7).
5. **Then**: hand the SSD to Rylee.

The smoke test step is non-negotiable. Testing on your own machine doesn't count — your machine already has Python, Ollama, and a hundred other things installed that mask failures. A clean Windows VM is the only honest test.

---

## 10. Open Questions Before Implementation

These are decisions only you can make:

1. **Branding the launcher** — should `START_STUDYING.bat` be renamed to something more Rylee-friendly (e.g., `Open Study Tool.bat`)? Spaces in filenames work fine in Windows.
2. **Custom icon** — currently the desktop shortcut uses the Python icon. Want to commission or pick a simple `.ico` instead? Optional.
3. **Auto-update path** — do you want a way to push updated lessons or app code to Rylee's SSD without re-shipping the whole thing? If yes, that's a v2 feature: a tiny `update.bat` that pulls from a private GitHub release. Not Phase 1.
4. **Telemetry** — would it help you to have the system silently log session summaries to a file you can read when you next see the SSD? Or is that a privacy line you don't want to cross? Default: no telemetry.
5. **Multiple SSDs** — are you giving Rylee a backup SSD? If so, the build script needs a `--clone-from` flag to mirror state between two drives. Probably out of scope for now.

Decide these and we can refine the spec before you hand any of this to Claude Code.

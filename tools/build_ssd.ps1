#Requires -Version 5.1
<#
.SYNOPSIS
    Builds the portable SSD runtime for the CSEC AI Study Partner.

.DESCRIPTION
    Copies the Python embeddable runtime, Ollama binaries, pip-installed dependencies,
    launcher files, and backend application to the target SSD.

    Does NOT touch 02_DATABASE, 03_KNOWLEDGE_BASE, 04_REPORTS, or 01_MODELS\Ollama.
    Those are data - handle separately.

.PARAMETER TargetDrive
    Drive letter ("E:") or full SSD root path ("E:\CSEC_AI_STUDY_PARTNER").
    Both forms are accepted and normalised.

.PARAMETER Force
    Re-install Python dependencies even if the lib\ folder already contains packages.

.EXAMPLE
    .\tools\build_ssd.ps1 -TargetDrive E:
    .\tools\build_ssd.ps1 -TargetDrive "F:\CSEC_AI_STUDY_PARTNER" -Force
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$TargetDrive,

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 0. Resolve paths
# ---------------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path   # .../tools/
$RepoRoot  = Split-Path -Parent $ScriptDir                     # .../CSEC-study-partner/

# Normalise -TargetDrive: accept "E:" or "E:\CSEC_AI_STUDY_PARTNER" (any trailing slash)
if ($TargetDrive -match '^[A-Za-z]:$') {
    $SsdRoot = $TargetDrive.ToUpper() + '\CSEC_AI_STUDY_PARTNER'
} elseif ($TargetDrive -match '^[A-Za-z]:\\') {
    $SsdRoot = $TargetDrive.TrimEnd('\')
} else {
    Write-Error "-TargetDrive must be a drive letter (E:) or full SSD root path (E:\CSEC_AI_STUDY_PARTNER)."
    exit 1
}

$ToolsRoot    = "$SsdRoot\01_TOOLS"
$PythonDir    = "$ToolsRoot\Python"
$PythonExe    = "$PythonDir\python.exe"
$LibDir       = "$ToolsRoot\lib"
$WheelsDir    = "$ToolsRoot\wheels"
$OllamaSrc    = "$env:LOCALAPPDATA\Programs\Ollama"
$OllamaDst    = "$ToolsRoot\Ollama"
$LaunchDir    = "$SsdRoot\00_LAUNCH"
$BackendDst   = "$SsdRoot\06_BACKEND"
$ReqFile      = "$RepoRoot\requirements.txt"
$TemplateDir  = "$RepoRoot\backend\launch_templates"
$GetPipUrl    = 'https://bootstrap.pypa.io/get-pip.py'
$GetPipTmp    = "$env:TEMP\csec_get-pip.py"

# ---------------------------------------------------------------------------
# Step result tracking
# ---------------------------------------------------------------------------
$Results = [ordered]@{}

function Set-Pass([string]$Name) {
    $Results[$Name] = '  [PASS]'
    Write-Host "  OK: $Name" -ForegroundColor Green
}

function Set-Fail([string]$Name, [string]$Msg) {
    $Results[$Name] = '  [FAIL]'
    Write-Host ""
    Write-Host "FAILED: $Name" -ForegroundColor Red
    Write-Host "  $Msg" -ForegroundColor Red
    Write-Host ""
    # Print whatever we have so far before stopping
    Show-Checklist
    exit 1
}

function Show-Checklist {
    Write-Host ""
    Write-Host "============================================================"
    Write-Host "  BUILD CHECKLIST"
    Write-Host "============================================================"
    foreach ($key in $Results.Keys) {
        $icon = if ($Results[$key] -eq '  [PASS]') { '  OK ' } else { '  !! ' }
        $col  = if ($Results[$key] -eq '  [PASS]') { 'Green' } else { 'Red' }
        Write-Host ("  {0}  {1}" -f $icon, $key) -ForegroundColor $col
    }
    Write-Host "============================================================"
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================"
Write-Host "  CSEC AI Study Partner - SSD Build Script"
Write-Host "  Target : $SsdRoot"
Write-Host "  Repo   : $RepoRoot"
Write-Host "  Force  : $Force"
Write-Host "============================================================"
Write-Host ""

# ---------------------------------------------------------------------------
# STEP 1 - Validate embeddable Python is in place
# ---------------------------------------------------------------------------
$s = "1. Validate embeddable Python"
Write-Host "STEP $s ..."

if (-not (Test-Path $PythonExe)) {
    Write-Host ""
    Write-Host "  [MISSING] python.exe not found at:" -ForegroundColor Yellow
    Write-Host "    $PythonExe" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  To fix - download and place the Python 3.11 embeddable runtime:"
    Write-Host ""
    Write-Host "    1. Open this URL in a browser:"
    Write-Host "       https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
    Write-Host "    2. Save the ZIP and expand it to:"
    Write-Host "       $PythonDir\"
    Write-Host "       (so that $PythonExe exists)"
    Write-Host "    3. Re-run this script."
    Write-Host ""
    Write-Host "  Do NOT use the full Python installer - use the embeddable ZIP only."
    Write-Host ""
    Set-Fail $s "python.exe missing - see instructions above"
}

# Verify it actually runs (capture stdout only - Python 3 prints version to stdout)
$ver = & $PythonExe --version
if ($LASTEXITCODE -ne 0) {
    Set-Fail $s "python.exe exists but failed to run: $ver"
}
Write-Host "  Found: $ver"
Set-Pass $s

# ---------------------------------------------------------------------------
# STEP 2 - Patch python311._pth so the lib\ folder is importable
# ---------------------------------------------------------------------------
$s = "2. Patch python311._pth"
Write-Host "STEP $s ..."

$pthFile = Get-ChildItem $PythonDir -Filter 'python3*._pth' | Select-Object -First 1
if ($null -eq $pthFile) {
    Set-Fail $s "No python3*._pth file found in $PythonDir"
}

$pthPath    = $pthFile.FullName
$pthContent = Get-Content $pthPath -Raw

$needsLib  = $pthContent -notmatch [regex]::Escape('..\lib')
$needsSite = $pthContent -match '#\s*import site'

if ($needsLib -or $needsSite) {
    # Uncomment 'import site' if it's commented out
    $updated = $pthContent -replace '#\s*import site', 'import site'
    # Append the relative lib path if not present
    if ($updated -notmatch [regex]::Escape('..\lib')) {
        $updated = $updated.TrimEnd() + "`r`n..\lib`r`n"
    }
    Set-Content -Path $pthPath -Value $updated -Encoding UTF8
    Write-Host "  Patched: $pthPath"
} else {
    Write-Host "  Already patched: $pthPath"
}
Set-Pass $s

# ---------------------------------------------------------------------------
# STEP 3 - Bootstrap pip into lib\
# ---------------------------------------------------------------------------
$s = "3. Bootstrap pip"
Write-Host "STEP $s ..."

$pipPresent = Test-Path "$LibDir\pip"
if ($pipPresent -and -not $Force) {
    Write-Host "  pip already present in $LibDir (use -Force to reinstall)"
    Set-Pass $s
} else {
    if (-not (Test-Path $LibDir)) {
        New-Item -ItemType Directory -Force -Path $LibDir | Out-Null
    }

    Write-Host "  Downloading get-pip.py from $GetPipUrl ..."
    try {
        Invoke-WebRequest $GetPipUrl -OutFile $GetPipTmp -UseBasicParsing
    } catch {
        Set-Fail $s "Failed to download get-pip.py: $_`n  (Internet access is required for this step.)"
    }

    Write-Host "  Installing pip into $LibDir ..."
    $env:PYTHONPATH = $LibDir
    & $PythonExe $GetPipTmp --target $LibDir --no-warn-script-location
    if ($LASTEXITCODE -ne 0) {
        Set-Fail $s "get-pip.py exited with code $LASTEXITCODE"
    }
    Remove-Item $GetPipTmp -Force -ErrorAction SilentlyContinue
    Set-Pass $s
}

# ---------------------------------------------------------------------------
# STEP 4 - Download wheels to SSD for offline recovery
# ---------------------------------------------------------------------------
$s = "4. Download wheels"
Write-Host "STEP $s ..."

if (-not (Test-Path $WheelsDir)) {
    New-Item -ItemType Directory -Force -Path $WheelsDir | Out-Null
}

$wheelCount = @(Get-ChildItem $WheelsDir -Filter '*.whl' -ErrorAction SilentlyContinue).Count
if ($wheelCount -gt 0 -and -not $Force) {
    Write-Host "  $wheelCount wheel(s) already in $WheelsDir (use -Force to re-download)"
    Set-Pass $s
} else {
    Write-Host "  Downloading wheels to $WheelsDir ..."
    $env:PYTHONPATH = $LibDir
    & $PythonExe -m pip download -r $ReqFile -d $WheelsDir
    if ($LASTEXITCODE -ne 0) {
        Set-Fail $s "pip download exited with code $LASTEXITCODE"
    }
    $wheelCount = @(Get-ChildItem $WheelsDir -Filter '*.whl').Count
    Write-Host "  $wheelCount wheel(s) downloaded"
    Set-Pass $s
}

# ---------------------------------------------------------------------------
# STEP 5 - Install dependencies into lib\ using wheels
# ---------------------------------------------------------------------------
$s = "5. Install Python dependencies"
Write-Host "STEP $s ..."

# Heuristic: fastapi package directory present = already installed
$fastapiPresent = Test-Path "$LibDir\fastapi"
if ($fastapiPresent -and -not $Force) {
    Write-Host "  Dependencies already installed in $LibDir (use -Force to reinstall)"
    Set-Pass $s
} else {
    Write-Host "  Installing from $ReqFile into $LibDir ..."
    $env:PYTHONPATH = $LibDir
    & $PythonExe -m pip install `
        -r $ReqFile `
        --target $LibDir `
        --find-links $WheelsDir `
        --no-warn-script-location
    if ($LASTEXITCODE -ne 0) {
        Set-Fail $s "pip install exited with code $LASTEXITCODE"
    }
    Set-Pass $s
}

# ---------------------------------------------------------------------------
# STEP 6 - Copy Ollama portable binary
# ---------------------------------------------------------------------------
$s = "6. Copy Ollama binary"
Write-Host "STEP $s ..."

$ollamaExeSrc = "$OllamaSrc\ollama.exe"
$ollamaLibSrc = "$OllamaSrc\lib"

if (-not (Test-Path $ollamaExeSrc)) {
    Write-Host ""
    Write-Host "  [MISSING] Ollama not found at $OllamaSrc" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  To fix:"
    Write-Host "    1. Install Ollama on this machine from https://ollama.com/download"
    Write-Host "    2. Re-run this script."
    Write-Host ""
    Set-Fail $s "ollama.exe not found at $ollamaExeSrc"
}

if (-not (Test-Path $OllamaDst)) {
    New-Item -ItemType Directory -Force -Path $OllamaDst | Out-Null
}

Write-Host "  Copying ollama.exe ..."
Copy-Item -Path $ollamaExeSrc -Destination "$OllamaDst\ollama.exe" -Force

if (Test-Path $ollamaLibSrc) {
    Write-Host "  Copying lib\ ..."
    if (-not (Test-Path "$OllamaDst\lib")) {
        New-Item -ItemType Directory -Force -Path "$OllamaDst\lib" | Out-Null
    }
    # robocopy exit codes 0-7 are success (0=nothing copied, 1=copied, 2=extras, etc.)
    robocopy $ollamaLibSrc "$OllamaDst\lib" /E /NFL /NDL /NJH /NJS /nc /ns /np
    if ($LASTEXITCODE -ge 8) {
        Set-Fail $s "robocopy of Ollama lib\ failed (exit code $LASTEXITCODE)"
    }
} else {
    Write-Host "  NOTE: No lib\ folder found at $ollamaLibSrc - skipping (may be fine on newer Ollama versions)"
}

Write-Host "  Ollama source: $OllamaSrc"
Set-Pass $s

# ---------------------------------------------------------------------------
# STEP 7 - Copy launcher files to 00_LAUNCH\ and START_STUDYING.bat to root
# ---------------------------------------------------------------------------
$s = "7. Copy launcher files"
Write-Host "STEP $s ..."

if (-not (Test-Path $TemplateDir)) {
    Set-Fail $s "launch_templates folder not found at $TemplateDir"
}

if (-not (Test-Path $LaunchDir)) {
    New-Item -ItemType Directory -Force -Path $LaunchDir | Out-Null
}

# The four 00_LAUNCH files
$launchFiles = @('first_run.bat', 'launch.bat', 'shutdown.bat', 'welcome.html')
foreach ($f in $launchFiles) {
    $src = "$TemplateDir\$f"
    if (-not (Test-Path $src)) {
        Set-Fail $s "Expected launcher file missing: $src"
    }
    Copy-Item -Path $src -Destination "$LaunchDir\$f" -Force
    Write-Host "  -> $LaunchDir\$f"
}

# START_STUDYING.bat goes to the SSD root
$startSrc = "$TemplateDir\START_STUDYING.bat"
if (-not (Test-Path $startSrc)) {
    Set-Fail $s "Expected launcher file missing: $startSrc"
}
Copy-Item -Path $startSrc -Destination "$SsdRoot\START_STUDYING.bat" -Force
Write-Host "  -> $SsdRoot\START_STUDYING.bat  [root entry point]"

Set-Pass $s

# ---------------------------------------------------------------------------
# STEP 8 - Copy backend app to 06_BACKEND\
# ---------------------------------------------------------------------------
$s = "8. Copy backend application"
Write-Host "STEP $s ..."

# What gets copied and where:
#
#   Repo: backend\          ->  SSD: 06_BACKEND\backend\
#   Repo: prompts\          ->  SSD: 06_BACKEND\prompts\
#
# app.py uses Path(__file__).resolve().parents[1] to find .env, which resolves
# to 06_BACKEND\ - the .env file is placed there by the user separately.
# launch.bat runs:  uvicorn backend.app:app --app-dir {SSD_ROOT}\06_BACKEND
# app.py adds its own parent dir to sys.path so bare module imports resolve.

$backendSrc   = "$RepoRoot\backend"
$backendCopy  = "$BackendDst\backend"
$promptsSrc   = "$RepoRoot\prompts"
$promptsCopy  = "$BackendDst\prompts"

if (-not (Test-Path $BackendDst)) {
    New-Item -ItemType Directory -Force -Path $BackendDst | Out-Null
}

# robocopy with exclusions: skip __pycache__, .pytest_cache, .claude, *.pyc, *.bak
Write-Host "  Copying backend\ -> $backendCopy ..."
robocopy $backendSrc $backendCopy `
    /E /PURGE `
    /XD __pycache__ .pytest_cache .claude `
    /XF "*.pyc" "*.bak" "*.bak2" `
    /NFL /NDL /NJH /NJS /nc /ns /np
if ($LASTEXITCODE -ge 8) {
    Set-Fail $s "robocopy of backend\ failed (exit code $LASTEXITCODE)"
}

Write-Host "  Copying prompts\ -> $promptsCopy ..."
robocopy $promptsSrc $promptsCopy `
    /E /PURGE `
    /XD __pycache__ `
    /XF "*.pyc" `
    /NFL /NDL /NJH /NJS /nc /ns /np
if ($LASTEXITCODE -ge 8) {
    Set-Fail $s "robocopy of prompts\ failed (exit code $LASTEXITCODE)"
}

# Copy requirements.txt alongside (useful for reference and rebuild)
Copy-Item -Path $ReqFile -Destination "$BackendDst\requirements.txt" -Force
Write-Host "  Copied requirements.txt -> $BackendDst\"

Write-Host ""
Write-Host "  NOTE: Place a .env file at $BackendDst\.env"
Write-Host "        with SSD-relative paths before first use."
Write-Host "        Use .env.example from the repo as the template."

Set-Pass $s

# ---------------------------------------------------------------------------
# STEP 9 - Unblock all files so SmartScreen doesn't flag them on Rylee's machine
# ---------------------------------------------------------------------------
$s = "9. Unblock-File (SmartScreen)"
Write-Host "STEP $s ..."

Write-Host "  Running Unblock-File over $SsdRoot (may take a moment) ..."
Get-ChildItem $SsdRoot -Recurse -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
Write-Host "  Done."
Set-Pass $s

# ---------------------------------------------------------------------------
# Final checklist
# ---------------------------------------------------------------------------
Show-Checklist

Write-Host "  Build complete."
Write-Host ""
Write-Host "  Remaining manual steps before shipping:"
Write-Host "    [1] Confirm $SsdRoot\02_DATABASE\csec.sqlite is present"
Write-Host "          and all 7 subjects are syllabus_locked = 1"
Write-Host "    [2] Confirm $SsdRoot\01_MODELS\Ollama\ contains"
Write-Host "          llama3.2:3b and nomic-embed-text blobs"
Write-Host "    [3] Place .env at $BackendDst\.env"
Write-Host "          (copy .env.example from the repo, set SSD_ROOT to the"
Write-Host "          drive letter the SSD will use on Rylee's machine - or"
Write-Host "          use a relative approach; see CLAUDE.md SSD Safety Rules)"
Write-Host "    [4] Test: run $SsdRoot\START_STUDYING.bat on a clean machine"
Write-Host "          with no Python or Ollama installed."
Write-Host ""

param(
    [string]$Url = "http://127.0.0.1:8000"
)

# Find Chrome (preferred: opens as a focused app window with no other tabs)
$chromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1

# Poll the server once per second until it responds (up to 90 seconds)
$ready = $false
for ($i = 0; $i -lt 90; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -ErrorAction Stop -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep 1
}

if (-not $ready) { exit 1 }  # server never came up; skip opening the browser

if ($chrome) {
    Start-Process $chrome "--app=$Url"
} else {
    Start-Process $Url
}

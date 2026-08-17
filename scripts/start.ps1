<#
.SYNOPSIS
    Starts the dashboard, refreshing first if there is no data yet.

.DESCRIPTION
    The browser is opened by the application itself, only after /healthz answers.
    Launching a browser before the backend is listening is what produces a
    spurious "connection refused" page and a bug report saying the app is broken.

.EXAMPLE
    .\scripts\start.ps1
    .\scripts\start.ps1 -Refresh
#>

[CmdletBinding()]
param(
    [switch]$Refresh,
    [int]$Port
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPy)) {
    Write-Host "No virtual environment found. Run .\scripts\setup.ps1 first." -ForegroundColor Red
    exit 1
}

if (-not $env:SRO_TRACKER_CONTACT -and -not (Test-Path (Join-Path $root "config.toml"))) {
    Write-Host "No contact address configured." -ForegroundColor Red
    Write-Host "The SEC requires a User-Agent with a real contact address." -ForegroundColor Yellow
    Write-Host ""
    Write-Host '  $env:SRO_TRACKER_CONTACT = "you@example.com"'
    Write-Host "  (or copy config.example.toml to config.toml and set 'contact')"
    exit 3
}

$dbPath = Join-Path $root "data\filings.db"
if ($Refresh -or -not (Test-Path $dbPath)) {
    if (-not (Test-Path $dbPath)) {
        Write-Host "No data yet - running the first refresh (about four minutes)." -ForegroundColor Cyan
    }
    & $venvPy -m sro_tracker.cli refresh
    $code = $LASTEXITCODE
    # 0 = clean, 2 = committed with degraded sources. Both leave usable data.
    if ($code -ne 0 -and $code -ne 2) {
        Write-Host ""
        Write-Host "Refresh did not commit (exit code $code)." -ForegroundColor Red
        if (Test-Path $dbPath) {
            Write-Host "Existing data is untouched; starting the dashboard anyway." -ForegroundColor Yellow
        } else {
            Write-Host "There is no data to display. Fix the errors above and retry." -ForegroundColor Red
            exit $code
        }
    }
}

$serveArgs = @("-m", "sro_tracker.cli", "serve", "--open")
if ($Port) { $serveArgs += @("--port", $Port) }

& $venvPy @serveArgs
exit $LASTEXITCODE

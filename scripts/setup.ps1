<#
.SYNOPSIS
    Creates the virtual environment and installs the tracker.

.DESCRIPTION
    Every external command is checked explicitly. PowerShell's
    $ErrorActionPreference = "Stop" does NOT stop on a failing native program -
    pip can fail while the script sails on and reports success. Invoke-Checked
    inspects $LASTEXITCODE after each call so a broken install is impossible to
    mistake for a working one.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
#>

[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv"

function Invoke-Checked {
    param(
        [Parameter(Mandatory)][string]$What,
        [Parameter(Mandatory)][scriptblock]$Command
    )
    Write-Host "  -> $What" -ForegroundColor DarkGray
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "SETUP FAILED: $What (exit code $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Write-Host "SRO Filing Tracker - setup" -ForegroundColor Cyan
Write-Host ""

# --- Python version gate -------------------------------------------------
# One minimum, enforced here, matching pyproject.toml. Not "3.10 in the docs,
# 3.11 in the README, any 3.x in the installer".
$MIN = [version]"3.11"

$versionText = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $versionText) {
    Write-Host "Could not run '$Python'. Install Python $MIN or newer, or pass -Python <path>." -ForegroundColor Red
    exit 1
}
$found = [version]$versionText.Trim()
if ($found -lt $MIN) {
    Write-Host "Python $found found, but $MIN or newer is required." -ForegroundColor Red
    exit 1
}
Write-Host "  Python $found" -ForegroundColor Green

# --- Virtual environment -------------------------------------------------
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Invoke-Checked "creating virtual environment" { & $Python -m venv $venv }
} else {
    Write-Host "  virtual environment already present" -ForegroundColor DarkGray
}

$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Virtual environment is incomplete: $venvPy is missing." -ForegroundColor Red
    exit 1
}

Invoke-Checked "upgrading pip" { & $venvPy -m pip install --quiet --upgrade pip }

$target = if ($Dev) { "$root[dev]" } else { $root }
Invoke-Checked "installing the tracker" { & $venvPy -m pip install --quiet -e $target }

# --- Verify the install actually works -----------------------------------
Invoke-Checked "verifying the install" { & $venvPy -c "import sro_tracker, flask, requests, bs4, openpyxl" }

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next:" -ForegroundColor Cyan
Write-Host '  $env:SRO_TRACKER_CONTACT = "you@example.com"   # required by the SEC'
Write-Host "  .\scripts\start.ps1"
Write-Host ""
Write-Host "A fresh clone has no data. start.ps1 will offer to run the first refresh."
exit 0

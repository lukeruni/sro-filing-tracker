<#
.SYNOPSIS
    Registers (or removes) the weekly Windows Scheduled Task.

.DESCRIPTION
    Runs `sro-tracker weekly`: refresh, build the report and workbook, deliver.

    Registered for the current user and run only when that user is logged on.
    That is intentional - the Outlook transport drives the desktop client, which
    needs an interactive session. For an unattended relay use the SMTP transport
    and pass -RunWhetherLoggedOn.

    The contact address is captured into the task at registration time, because
    a scheduled task does not inherit your interactive shell's environment.
    Anything already in config.toml is used in preference.

.EXAMPLE
    .\scripts\install_schedule.ps1 -Time 07:30 -Day Monday
    .\scripts\install_schedule.ps1 -Remove
#>

[CmdletBinding()]
param(
    [string]$TaskName = "SRO Filing Tracker - Weekly Report",
    [ValidateSet("Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday")]
    [string]$Day = "Monday",
    [string]$Time = "07:30",
    [string]$Contact = $env:SRO_TRACKER_CONTACT,
    [switch]$RunWhetherLoggedOn,
    [switch]$NoSend,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $root ".venv\Scripts\python.exe"

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "No task named '$TaskName' is registered." -ForegroundColor Yellow
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $venvPy)) {
    Write-Host "No virtual environment found. Run .\scripts\setup.ps1 first." -ForegroundColor Red
    exit 1
}

$hasConfig = Test-Path (Join-Path $root "config.toml")
if (-not $Contact -and -not $hasConfig) {
    Write-Host "No contact address available." -ForegroundColor Red
    Write-Host "A scheduled task does not inherit your shell environment, so the" -ForegroundColor Yellow
    Write-Host "address must be captured now or set in config.toml." -ForegroundColor Yellow
    Write-Host ""
    Write-Host '  .\scripts\install_schedule.ps1 -Contact "you@example.com"'
    exit 3
}

# Wrap in cmd so stdout and stderr land in a dated log. A scheduled task that
# fails silently is indistinguishable from one that never ran.
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$weeklyArgs = "-m sro_tracker.cli weekly"
if ($NoSend) { $weeklyArgs += " --no-send" }

$inner = "`"$venvPy`" $weeklyArgs"
$logFile = Join-Path $logDir "weekly-%DATE:~-4%%DATE:~4,2%%DATE:~7,2%.log"
$command = "/c cd /d `"$root`" && $inner >> `"$logFile`" 2>&1"

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $command -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

# StartWhenAvailable matters: a laptop asleep at 07:30 on Monday still runs the
# report when it wakes, instead of skipping the week entirely.

if ($RunWhetherLoggedOn) {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
}

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Weekly SRO rule filing refresh and report."

if ($Contact) {
    # Environment for the task process. cmd sets it before python starts.
    $command = "/c cd /d `"$root`" && set SRO_TRACKER_CONTACT=$Contact && $inner >> `"$logFile`" 2>&1"
    $task.Actions = @(New-ScheduledTaskAction -Execute "cmd.exe" -Argument $command -WorkingDirectory $root)
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Replacing the existing task." -ForegroundColor DarkGray
}

Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName'." -ForegroundColor Green
Write-Host "  runs      $Day at $Time"
Write-Host "  command   $venvPy $weeklyArgs"
Write-Host "  logs      $logDir"
Write-Host "  delivery  $(if ($NoSend) { 'preview only (--no-send)' } else { 'per config.toml mail_transport' })"
Write-Host ""
Write-Host "Test it now without waiting for the schedule:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host ""
Write-Host "Remove it with:" -ForegroundColor Cyan
Write-Host "  .\scripts\install_schedule.ps1 -Remove"
exit 0

<#
.SYNOPSIS
Register (or remove) the Monday 09:00 weekly preview as a Windows scheduled task.
.DESCRIPTION
The task runs `pulse weekly --confirm-public-source`: fetch public reviews, import, snapshot and
preview. It NEVER sends email or edits a Doc. Read artifacts/<run_id>/pulse.md afterwards, then
publish by hand with `pulse publish-direct <run_id> --send --approve`.
Nothing is installed unless you pass -Install. Times are the PC's local time (set the PC to IST).
.EXAMPLE
.\scripts\schedule.ps1 -DryRun            # show what would be registered
.\scripts\schedule.ps1 -Install           # register "GrowwPulseWeekly"
.\scripts\schedule.ps1 -Uninstall         # remove it
.\scripts\schedule.ps1 -Status            # last weekly status (codes only)
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$DryRun,
    [string]$Config = 'config/settings.groq.example.yaml',
    [string]$At = '09:00'
)

$ErrorActionPreference = 'Stop'
$taskName = 'GrowwPulseWeekly'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$statusFile = Join-Path $projectRoot 'data\logs\weekly_status.json'

if ($Status) {
    if (Test-Path -LiteralPath $statusFile) { Get-Content -LiteralPath $statusFile } else { 'No weekly run recorded yet.' }
    exit 0
}
if ($Uninstall) {
    if ($DryRun) { "Would remove scheduled task $taskName"; exit 0 }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    "Removed $taskName"; exit 0
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Project Python is missing. See README.md setup.' }

$arguments = "-m groww_pulse --config `"$Config`" weekly --confirm-public-source"
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)

if ($DryRun -or -not $Install) {
    "Task: $taskName | Mondays at $At | $python $arguments"
    'Not installed. Add -Install to register it.'
    exit 0
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Groww Pulse weekly preview (never sends)' -Force | Out-Null
"Registered $taskName (Mondays $At). Check results with: .\scripts\schedule.ps1 -Status"

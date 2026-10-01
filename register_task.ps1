# Registers "JobHuntingBuddy" to run one poll cycle every 15 minutes.
# Run from an elevated OR normal PowerShell:  .\register_task.ps1
# Remove with:  Unregister-ScheduledTask -TaskName JobHuntingBuddy -Confirm:$false

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$cmd  = Join-Path $root "run_poll.cmd"

if (-not (Test-Path $cmd)) { throw "run_poll.cmd not found at $cmd" }

$action  = New-ScheduledTaskAction -Execute $cmd
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
             -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet `
             -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
             -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "JobHuntingBuddy" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Poll SimplifyJobs + JobSpy for new-grad SWE/ML roles" `
    -Force | Out-Null

Write-Host "Registered 'JobHuntingBuddy' - runs every 15 minutes."
Write-Host "  Status : Get-ScheduledTask JobHuntingBuddy"
Write-Host "  Run now: Start-ScheduledTask JobHuntingBuddy"
Write-Host "  Log    : $root\data\poll.log"
Write-Host "  Remove : Unregister-ScheduledTask JobHuntingBuddy -Confirm:`$false"

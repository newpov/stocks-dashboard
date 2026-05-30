# One-shot registration of the daily-rebuild Windows Task Scheduler entry.
#
# Run once per machine, as the user account that owns the repo:
#   powershell -ExecutionPolicy Bypass -File setup_scheduled_task.ps1
#
# Re-running is safe: the script removes any prior entry with the same name
# before registering, so it acts as an idempotent install.
#
# What this creates:
#   - Task name:    StocksDashboardDailyRebuild
#   - Trigger:      Every day at 09:30 local time
#   - Action:       Invoke daily_rebuild.ps1 in this repo folder
#   - Settings:     Run missed schedules on boot, 10-min hard cap, retry x2
#   - Conditions:   Network required, wake-to-run disabled by default

$taskName = "StocksDashboardDailyRebuild"
$repo     = $PSScriptRoot
$script   = Join-Path $repo "daily_rebuild.ps1"

if (-not (Test-Path $script)) {
    Write-Error "daily_rebuild.ps1 not found at $script - aborting."
    exit 1
}

# Remove any prior registration so re-runs are idempotent
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task '$taskName'..."
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -Daily -At "09:30"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartInterval (New-TimeSpan -Minutes 30) `
    -RestartCount 2

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Rebuilds docs/index.html from log.xlsx daily and pushes to GitHub. See daily_rebuild.ps1." | Out-Null

Write-Host ""
Write-Host "Task '$taskName' registered. Verify with:"
Write-Host "  Get-ScheduledTask -TaskName $taskName"
Write-Host "Run it now to test:"
Write-Host "  Start-ScheduledTask -TaskName $taskName"
Write-Host ""
Write-Host "Logs append to: $(Join-Path $repo 'daily_rebuild.log')"

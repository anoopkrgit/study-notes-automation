# win-install-setup.ps1
# Windows installation helper invoked by 1-click install.bat
#
# DESIGN NOTE: This installer registers the Task Scheduler action to run the
# pipeline directly from THIS repo's own scripts/ folder. This means
# src/config/templates edits take effect on the very next nightly run with
# no reinstall step. Only re-run this installer if the repo itself moves
# to a different path, or to re-register the task from scratch.
#
# -PipelineArgs is passed explicitly below (the long-standing nightly
# policy: assemble WITH the LLM, generate preview only) because
# wsl-study-notes-processor.sh no longer has an implicit no-args nightly
# fallback of its own -- see that script's own comments. Without this,
# the registered task would run "$@"-empty, main.py's own
# --stage1-mode/--stage2-mode defaults ("off") would apply, and the
# nightly task would silently do nothing every night.

$ErrorActionPreference = "Continue"

Write-Host "====================================================================" -ForegroundColor Cyan
Write-Host "Installing Study Notes Automation to run directly from this folder..." -ForegroundColor Cyan

$ScriptSource = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot = Split-Path -Parent $ScriptSource

# 1. Register Task Scheduler Task (3:00 AM Daily Trigger)
Write-Host "Registering 3:00 AM Windows Task Scheduler task ('StudyNotesNightly')..." -ForegroundColor Yellow
try {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RepoRoot\scripts\win-environment-setup.ps1`" -PipelineArgs `"--stage1-mode llm-full --stage2-mode no-llm`""
    $trigger = New-ScheduledTaskTrigger -Daily -At "03:00 AM"
    # -DontStopIfGoingOnBatteries: without this, Windows' default behaviour
    #   is to STOP the task partway through if the laptop switches to
    #   battery power mid-run, silently killing an in-progress generation.
    # -MultipleInstances IgnoreNew: without this, if the one-time
    #   'StudyNotesRetry' wake task (registered by win-environment-setup.ps1
    #   after a rate limit) ever fires close to this daily task, Windows'
    #   default is to let both run at once -- two processes writing the same
    #   state file concurrently. IgnoreNew makes a second attempt a no-op
    #   instead, so only one copy of the pipeline is ever running.
    # -ExecutionTimeLimit: 2 hours, matching win-environment-setup.ps1's own
    #   internal 115-minute (6900s) WSL timeout by design -- that 5-minute
    #   margin is what lets the internal timeout fire and clean up (force-kill
    #   wsl.exe + terminate the WSL distro) *before* Task Scheduler would ever
    #   need to hard-kill the whole task. This was previously bumped to 3
    #   hours here without updating the internal timeout to match, which
    #   silently widened that margin to 65 minutes of slack -- exactly why a
    #   hung run took a full 3 hours to get force-killed instead of ~115
    #   minutes.
    $settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName 'StudyNotesNightly' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Task Scheduler task 'StudyNotesNightly' registered successfully, running from $RepoRoot." -ForegroundColor Green
} catch {
    Write-Host "WARNING: Failed to register Task Scheduler task: $_" -ForegroundColor Red
}

# 2. Invoke WSL Linux installer (venv, dependencies, sudoers remount helper)
Write-Host "Invoking WSL linux installer (install.sh)..." -ForegroundColor Yellow
$WslRepoRoot = ($RepoRoot -replace '^([A-Za-z]):', { '/mnt/' + $_.Groups[1].Value.ToLower() }) -replace '\\', '/'
wsl.exe -e bash -c "bash '$WslRepoRoot/install.sh'"

Write-Host "====================================================================" -ForegroundColor Cyan
Write-Host "Windows installation setup finished!" -ForegroundColor Green

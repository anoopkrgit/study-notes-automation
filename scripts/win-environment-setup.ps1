# win-environment-setup.ps1
# Windows Host Scaffolding: Power lock, Google Drive process/mount check, and Task Scheduler retry handler
#
# -PipelineArgs forwards straight through to wsl-study-notes-processor.sh,
# which forwards it straight through to main.py's own CLI flags (see
# `python3 src/main.py --help`, e.g. --stage1-impl/--stage2-impl
# {legacy,graph,subprocess} and --stage1-mode/--stage2-mode
# {off,no-llm,llm-token-saver,llm-full}). This is the ONLY thing that
# decides which pipeline implementation runs -- there's no env var or
# config file to edit. Left empty, main.py's own --stage1-mode/
# --stage2-mode defaults ("off") apply and BOTH stages are off -- the
# wrapper script no longer has an implicit nightly fallback of its own
# (see wsl-study-notes-processor.sh's own comments for why that was
# removed). The registered 'StudyNotesNightly' Scheduled Task must be
# given an explicit -PipelineArgs for the nightly policy to actually run
# anything -- see docs/setup-guide.md's registration examples.
#
# Example -- run the agentic pipeline for real (spends tokens):
#   .\win-environment-setup.ps1 -PipelineArgs "--stage1-impl graph --stage2-impl graph --stage1-mode llm-full --stage2-mode llm-full"
# Example -- same, but cheap smoke test instead of a real run:
#   .\win-environment-setup.ps1 -PipelineArgs "--stage1-impl graph --stage2-impl graph --stage1-mode llm-token-saver --stage2-mode llm-token-saver"
param(
    [string]$PipelineArgs = ""
)

$ErrorActionPreference = "Continue"
$ScriptSource = $PSScriptRoot
if (-not $ScriptSource) { $ScriptSource = Split-Path -Parent $MyInvocation.MyCommand.Definition }
$LocalRoot = Split-Path -Parent $ScriptSource
$LogDir    = "$LocalRoot\logs"
$LogFile   = "$LogDir\study-notes-pipeline.log"
$StateDir  = "$LocalRoot\state"
$EpochFile = "$StateDir\retry-epoch.txt"
$FallbackLogFile = "$env:TEMP\studynotes-fallback.log"
$ScriptStart = Get-Date

# Ensure directories exist
if (-not (Test-Path $LogDir))   { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }

function Log-Message([string]$msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')  [WIN-HOST]  $msg"
    Write-Host $line
    try {
        Add-Content -Path $LogFile -Value $line -Encoding UTF8 -ErrorAction Stop
    } catch {
        # Primary log write failed (e.g. the log directory itself is part of
        # what's unhealthy that night) -- fall back to a second, independent
        # local path so at least one write is likely to land, instead of the
        # whole run going silent with no trace at all.
        try { Add-Content -Path $FallbackLogFile -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue } catch {}
    }
}

# Untagged separator line bracketing one whole run cycle (WIN-HOST launch
# through final screen-off/failure decision), so a human scanning the log
# file can see where one night's run ends and the next begins at a glance --
# WITH a timestamp on the OPENING banner (so you don't have to hunt down to
# the first tagged line just to see when a run started), and a plain closing
# banner of the same width once that run cycle is done. Same write-with-
# fallback behavior as Log-Message, deliberately duplicated rather than
# calling Log-Message here, since a banner line has no "[WIN-HOST]" tag.
function Log-Banner([string]$centerText = $null) {
    $width = 85
    if ($centerText) {
        $inner = " $centerText "
        $padTotal = $width - $inner.Length
        $padLeft = [math]::Ceiling($padTotal / 2)
        $padRight = $padTotal - $padLeft
        $line = ("=" * $padLeft) + $inner + ("=" * $padRight)
    } else {
        $line = "=" * $width
    }
    Write-Host $line
    try {
        Add-Content -Path $LogFile -Value $line -Encoding UTF8 -ErrorAction Stop
    } catch {
        try { Add-Content -Path $FallbackLogFile -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue } catch {}
    }
}

Log-Banner (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
Log-Message "Windows host environment setup starting..."

# 1. Compile C# Win32 Utils & Assert Keep-Awake
try {
    $csharpCode = @'
    using System;
    using System.Runtime.InteropServices;

    public struct LASTINPUTINFO {
        public uint cbSize;
        public uint dwTime;
    }

    public class Win32Utils {
        [DllImport("kernel32.dll", CharSet=CharSet.Auto, SetLastError=true)]
        public static extern uint SetThreadExecutionState(uint esFlags);

        [DllImport("user32.dll")]
        public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);

        [DllImport("user32.dll", CharSet=CharSet.Auto)]
        public static extern IntPtr SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

        public static uint GetLastInputTick() {
            LASTINPUTINFO lii = new LASTINPUTINFO();
            lii.cbSize = (uint)Marshal.SizeOf(lii);
            GetLastInputInfo(ref lii);
            return lii.dwTime;
        }
    }
'@
    Add-Type -TypeDefinition $csharpCode -ErrorAction Stop
    Log-Message "Asserting keep-awake power lock..."
    $ES_CONTINUOUS      = [uint32]2147483648
    $ES_SYSTEM_REQUIRED = [uint32]1
    [void][Win32Utils]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)
    Log-Message "Keep-awake lock asserted successfully."

    # 1.5. Settling Delay (Now Safe under Power Lock) -- the keep-awake lock
    # above is already held, so it's safe to deliberately wait here for OS
    # services (network, virtual drives, WSL) to finish settling right after
    # a wake from Modern Standby, instead of racing into drive checks/WSL
    # while they're still cold. Restored from the older Run-StudyNotesNightly.ps1
    # design (SETUP-STATUS.md item 17) after being dropped in this rewrite --
    # its absence is the likely proximate trigger of a 3h hang with zero log
    # output, since the system was still flapping in/out of Modern Standby
    # when this script raced straight into the Google Drive check.
    Log-Message "Power lock active; pausing 20s for OS services to settle..."
    Start-Sleep -Seconds 20
    Log-Message "Settling delay completed."
} catch {
    Log-Message "WARNING: Win32 keep-awake assertion failed: $_"
}

# 2. Clean up any stale one-time retry task from a PREVIOUS rate-limited run.
# If a run hit a retryable API error, step 6 below registers a one-time
# 'StudyNotesRetry' wake task for later. Once that retry actually happens
# (this run IS that retry, or a normal 3 AM run already resumed the
# chapter successfully some other way), the one-time task has served its
# purpose and must be removed -- otherwise it could still be sitting
# registered for a stale time and fire again unexpectedly later.
# -ErrorAction SilentlyContinue: this is a no-op (not an error) on a normal
# night when no retry was ever scheduled.
Log-Message "Unregistering any stale StudyNotesRetry wake task..."
Unregister-ScheduledTask -TaskName 'StudyNotesRetry' -Confirm:$false -ErrorAction SilentlyContinue

# 3. Check & Relaunch Google Drive for Desktop
Log-Message "Checking Google Drive process..."
$driveProc = Get-Process -Name "GoogleDriveFS" -ErrorAction SilentlyContinue
if (-not $driveProc) {
    Log-Message "GoogleDriveFS not running; attempting launch..."
    $driveExe = Get-ChildItem "C:\Program Files\Google\Drive File Stream\*\GoogleDriveFS.exe" -ErrorAction SilentlyContinue |
        Sort-Object { [version]($_.Directory.Name) } -Descending |
        Select-Object -First 1 -ExpandProperty FullName
    if ($driveExe) {
        Start-Process -FilePath $driveExe
        Log-Message "Launched $driveExe"
    } else {
        Log-Message "WARNING: Could not locate GoogleDriveFS.exe"
    }
} else {
    Log-Message "GoogleDriveFS running (PID $($driveProc.Id))"
}

# 4. Poll for G:\ drive
Log-Message "Checking G:\ drive availability..."
$driveReady = $false
for ($i = 0; $i -lt 60; $i++) {
    if (Test-Path "G:\") { $driveReady = $true; break }
    Start-Sleep -Seconds 1
}
Log-Message "G:\ drive availability: $driveReady (waited ${i}s)"

# 5. Launch WSL Processor Script
#
# NOTE: previously wrapped in Start-Job/Wait-Job/Stop-Job. Stop-Job frequently
# cannot kill a wsl.exe call that's blocked in a native/synchronous call --
# exactly what happens on a flapping-Modern-Standby host -- so a timeout here
# never actually stopped anything; the run just sat wedged until Task
# Scheduler's own outer ExecutionTimeLimit force-killed the whole task hours
# later. Start-Process gives a real OS process handle that Stop-Process can
# actually terminate, and wsl.exe --terminate is a hard fallback for the case
# where killing the wsl.exe frontend doesn't stop the command still running
# inside the persistent WSL2 VM (wsl.exe is a thin RPC client, not the actual
# process owner).
$WslLocalRoot = $LocalRoot -replace '\\', '/'
if ($WslLocalRoot -match '^([A-Za-z]):(.*)') {
    $WslLocalRoot = "/mnt/$($Matches[1].ToLower())$($Matches[2])"
}
$wslCommand = "$WslLocalRoot/scripts/wsl-study-notes-processor.sh"
if ($PipelineArgs) { $wslCommand = "$wslCommand $PipelineArgs" }
Log-Message "Launching WSL study notes processor: wsl.exe -e bash -lc `"$wslCommand`""
$jobExit = 1
try {
    $proc = Start-Process -FilePath "wsl.exe" -ArgumentList @('-e', 'bash', '-lc', $wslCommand) -PassThru -WindowStyle Hidden
    Log-Message "WSL processor launched (PID $($proc.Id)); waiting up to 115m..."

    $timeoutSeconds = 6900
    $pollSeconds    = 60
    $waited         = 0
    $timedOut       = $true
    while ($waited -lt $timeoutSeconds) {
        if ($proc.WaitForExit($pollSeconds * 1000)) {
            $timedOut = $false
            break
        }
        $waited += $pollSeconds
        Log-Message "WSL job still running (${waited}s elapsed)..."
    }

    if (-not $timedOut) {
        $jobExit = $proc.ExitCode
        Log-Message "WSL processor finished with exit code $jobExit"
    } else {
        Log-Message "CRITICAL TIMEOUT: WSL job exceeded 115m. Force-killing wsl.exe (PID $($proc.Id))..."
        try {
            Stop-Process -Id $proc.Id -Force -ErrorAction Stop
            Log-Message "wsl.exe (PID $($proc.Id)) force-killed."
        } catch {
            Log-Message "WARNING: Stop-Process on wsl.exe failed: $_"
        }
        try {
            $distro = (wsl.exe -l -q 2>$null | Select-Object -First 1)
            if ($distro) { $distro = $distro.Trim() }
            if ($distro) {
                Log-Message "Terminating WSL distro '$distro' as hard-kill fallback (frontend kill alone doesn't stop work still running inside the VM)..."
                wsl.exe --terminate "$distro" | Out-Null
                Log-Message "WSL distro '$distro' terminated."
            } else {
                Log-Message "WARNING: Could not resolve WSL distro name; skipping --terminate fallback."
            }
        } catch {
            Log-Message "WARNING: wsl.exe --terminate fallback failed: $_"
        }
        $jobExit = -1
    }
} catch {
    Log-Message "Error executing wsl.exe: $_"
}

# 6. Rate Limit Exit Code (42) Handling
if ($jobExit -eq 42) {
    Log-Message "Job exit code 42 (Rate Limited); checking retry epoch..."
    if (Test-Path $EpochFile) {
        $epochRaw = (Get-Content -Path $EpochFile -ErrorAction SilentlyContinue).Trim()
        if ($epochRaw -match '^\d+$') {
            try {
                $retryAt = [DateTimeOffset]::FromUnixTimeSeconds([int64]$epochRaw).LocalDateTime
                # Carry this run's -PipelineArgs into the retry so a rate-limited
                # graph/live run resumes the same way instead of silently dropping
                # to an empty $PipelineArgs -- which, with the wrapper's no-args
                # fallback removed, would mean both stages default to "off" and
                # the retry would do nothing at all.
                $retryArgString = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`""
                if ($PipelineArgs) { $retryArgString += " -PipelineArgs `"$PipelineArgs`"" }
                $retryAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $retryArgString
                $retryTrigger = New-ScheduledTaskTrigger -Once -At $retryAt
                $retrySettings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
                $retryPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
                Register-ScheduledTask -TaskName 'StudyNotesRetry' -Action $retryAction -Trigger $retryTrigger -Settings $retrySettings -Principal $retryPrincipal -Force | Out-Null
                Log-Message "Scheduled wake retry for $retryAt"
            } catch {
                Log-Message "Failed to register retry task: $_"
            }
        }
    }
    $jobExit = 0
}

# 7. Handle genuine job failure -- MUST NOT sleep the machine on a failure.
#
# WHY THIS STEP EXISTS: without it, a run that genuinely failed (WSL
# unreachable, a Python exception, the 115-minute internal timeout, ...)
# falls straight through to the "turn off screen" step below exactly like a
# successful run would. That previously caused a real incident: a failed
# run put the laptop to sleep in the middle of the user actively typing on
# it, because the failure was silently treated the same as success. Only
# jobExit -eq 0 (a real success) or jobExit -eq 42 (rate-limited, and
# already normalized to 0 in step 6 above once a retry was scheduled) may
# proceed to the sleep step. Any other exit code stops here, releases the
# keep-awake lock, and leaves the machine ON so the failure is visible
# instead of hidden.
if ($jobExit -ne 0) {
    [void][Win32Utils]::SetThreadExecutionState($ES_CONTINUOUS)
    Log-Message "Job FAILED (exit $jobExit); leaving machine awake for inspection. Not sleeping."
    Log-Message "Total elapsed: $([math]::Round(((Get-Date) - $ScriptStart).TotalSeconds))s."
    Log-Banner
    Write-Host "Study notes job FAILED (exit $jobExit). Machine left awake." -ForegroundColor Red
    exit 1
}

# 8. User activity check & screen off
Log-Message "Monitoring user activity for 30s before release..."
$userActive = $false
$baseline = [Win32Utils]::GetLastInputTick()
for ($elapsed = 0; $elapsed -lt 30; $elapsed++) {
    Start-Sleep -Seconds 1
    if ([Win32Utils]::GetLastInputTick() -ne $baseline) { $userActive = $true; break }
}

[void][Win32Utils]::SetThreadExecutionState($ES_CONTINUOUS)
if ($userActive) {
    Log-Message "Local user activity detected; skipping screen off."
} else {
    Log-Message "No user activity; turning off screen (Modern Standby)."
    [void][Win32Utils]::SendMessage([IntPtr]0xffff, 0x0112, [IntPtr]0xF170, [IntPtr]2)
}

Log-Message "Windows host environment setup complete. Total elapsed: $([math]::Round(((Get-Date) - $ScriptStart).TotalSeconds))s."
Log-Banner

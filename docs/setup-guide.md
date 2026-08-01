# Setup Guide

This guide covers machine onboarding, environment configuration, and Task Scheduler installation.

## 1-Click Machine Onboarding

To deploy this project on any Windows machine with WSL Ubuntu installed:

1. Open Windows Explorer and navigate to your Google Drive backup folder.
2. Double-click `install.bat`.
3. Approve the Windows User Account Control (UAC) prompt.

The installer will automatically:
- Build the Python virtual environment (`~/.global_venv`) and install dependencies
- Register the 3:00 AM Task Scheduler task (`StudyNotesNightly`) automatically, pointing directly to the scripts in this repository.

## Setting Up API Keys

This pipeline calls the Anthropic API directly (no `claude` CLI login needed), billed
per token against your own API key. Create an environment file at `~/.anthropic_env`
in WSL Ubuntu:

```bash
cat << 'EOF' > ~/.anthropic_env
ANTHROPIC_API_KEY=sk-ant-api03-...
EOF
chmod 600 ~/.anthropic_env
```

## Toolchain Dependencies

`scripts/install.sh` installs the Python packages in `requirements.txt` plus the
Node.js `docx` package (globally, via `npm install -g docx` — this is what the
generator's build script uses to produce the actual `.docx` file). It also checks
for `node`, `soffice` (LibreOffice, used for SVG→PDF diagram conversion) and
`pdftoppm` (from `poppler-utils`, used for PDF→PNG conversion and the page-by-page
visual accuracy check) — install any it reports missing via `sudo apt install
nodejs libreoffice poppler-utils`.

`openpyxl` (in `requirements.txt`) is optional but recommended: if a
`Syllabus-PCM-BT.xlsx` spreadsheet exists in the Telegram-download project's
`general-files/` folder, the chapter classifier will read up-to-date chapter
names/numbers from it. If it's absent, classification still works correctly using
the built-in chapter maps in `src/func_classify_and_rename.py`.

## Sudo Passwordless Remount Setup

`scripts/install.sh` sets this up automatically (it will prompt for your sudo password
once, interactively, the same way `sudo apt install` would) — this lets
`wsl-study-notes-processor.sh` auto-heal a stale Google Drive mount unattended, at
3 AM, with nobody there to type a password. It validates the sudoers rule with
`visudo -c` before installing it, so a mistake here can't break `sudo` system-wide.

If you'd rather set it up by hand instead (e.g. `install.sh` reported a `[WARNING]`
because `sudo` wasn't available in that shell), run:

```bash
sudo install -m 0755 -o root -g root scripts/remount-gdrive /usr/local/sbin/remount-gdrive

# Validate BEFORE installing -- an invalid sudoers file can lock you out of sudo:
echo "$USER ALL=(ALL) NOPASSWD: /usr/local/sbin/remount-gdrive" > /tmp/study-notes-remount.tmp
sudo visudo -c -f /tmp/study-notes-remount.tmp && \
    sudo install -m 0440 -o root -g root /tmp/study-notes-remount.tmp /etc/sudoers.d/study-notes-remount
rm -f /tmp/study-notes-remount.tmp
```

Verify it worked (should list the rule without asking for a password):
```bash
sudo -n -l
```

## Log Monitoring

All system output is written to a single unified log file in the repository's `logs/` directory:
- Windows Path: `<repository-root>\logs\study-notes-pipeline.log`
- Linux Path: `<wsl-repo-root>/logs/study-notes-pipeline.log`

## Switching the Nightly Pipeline Mode (Legacy vs. Agentic)

Which implementation `StudyNotesNightly` runs is a pure CLI choice, forwarded from the
Task Scheduler action's arguments, through `win-environment-setup.ps1`'s `-PipelineArgs`
parameter, through `wsl-study-notes-processor.sh`, down to `main.py`'s own
`--stage1-impl`/`--stage2-impl {legacy,graph,subprocess}` and
`--stage1-mode`/`--stage2-mode {off,no-llm,llm-token-saver,llm-full}` flags (see
`python3 src/main.py --help`). The two `--stageN-mode` flags are fully independent per
stage — e.g. Stage 1 can run `llm-full` while Stage 2 runs `llm-token-saver` in the same
invocation. No source file, env var, or config needs editing to change modes — only the
registered task's action.

Run one of these three blocks in an elevated PowerShell prompt to select a mode. Each
one fully replaces the task's action; only run the one you want active.

**1. Legacy (default) — Stage 1 with the LLM router, Stage 2 mock/zero-token:**

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"<YOUR_PROJECT_DIRECTORY>\scripts\win-environment-setup.ps1`""
Set-ScheduledTask -TaskName 'StudyNotesNightly' -Action $action
```

**2. Agentic, dummy prompts** — new multi-agent graph pipeline, real API calls but
capped/cheap (`DEV_TOKEN_SAVER_MODE`, via `llm-token-saver`); a smoke test, produces a
placeholder `.docx`, not usable notes:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"<YOUR_PROJECT_DIRECTORY>\scripts\win-environment-setup.ps1`" -PipelineArgs `"--stage1-impl graph --stage2-impl graph --stage1-mode llm-token-saver --stage2-mode llm-token-saver`""
Set-ScheduledTask -TaskName 'StudyNotesNightly' -Action $action
```

**3. Agentic, real prompts** — full graph pipeline run, spends real tokens, produces a
real `.docx`:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"<YOUR_PROJECT_DIRECTORY>\scripts\win-environment-setup.ps1`" -PipelineArgs `"--stage1-impl graph --stage2-impl graph --stage1-mode llm-full --stage2-mode llm-full`""
Set-ScheduledTask -TaskName 'StudyNotesNightly' -Action $action
```

Check which mode is currently registered at any time:

```powershell
(Get-ScheduledTask -TaskName 'StudyNotesNightly').Actions.Arguments
```

**Caveats:**
- `win-install-setup.ps1` re-registers this task with `-Force` and no arguments
  whenever it runs (e.g. a re-run of `install.bat`), silently reverting to mode 1. If a
  non-legacy mode needs to survive a reinstall, that script's own `$action` definition
  needs the same `-PipelineArgs` appended.
- The rate-limit retry task (`StudyNotesRetry`, registered automatically by
  `win-environment-setup.ps1` on exit code 42) carries the same `-PipelineArgs`
  forward, so a rate-limited run resumes in the same mode it started in.
- Per `docs/migration-to-agents.md`'s Verification Plan, don't trust mode 3 unattended
  until steps 1–6 (unit tests, mock-mode wiring, `DEV_TOKEN_SAVER_MODE` smoke test, a
  supervised `--live` A/B run, crash/resume test, exit-code contract test) have passed.
  Mode 2 is the safe way to validate the graph wiring nightly before that.

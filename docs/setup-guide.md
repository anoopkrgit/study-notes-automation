# Setup Guide

This guide covers machine onboarding, environment configuration, and Task Scheduler installation.

## 1-Click Machine Onboarding

To deploy this project on any Windows machine with WSL Ubuntu installed:

1. Open Windows Explorer and navigate to your Google Drive backup folder.
2. Double-click `install.bat`.
3. Approve the Windows User Account Control (UAC) prompt.

The installer will automatically:
- Create `C:\StudyNotesAutomation\` (with `logs` and `state` subfolders)
- Copy `win-environment-setup.ps1` and `wsl-study-notes-processor.sh` to `C:\StudyNotesAutomation\`
- Build the Python virtual environment (`~/.global_venv`) and install dependencies
- Register the 3:00 AM Task Scheduler task (`StudyNotesNightly`) automatically

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

All system output is written to a single unified log file accessible from both Windows and Linux:
- Windows Path: `C:\StudyNotesAutomation\logs\study-notes-pipeline.log`
- Linux Path: `/mnt/c/StudyNotesAutomation/logs/study-notes-pipeline.log`

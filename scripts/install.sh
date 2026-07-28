#!/usr/bin/env bash
# install.sh
# Automated Linux dependency installer & venv bootstrap script

set -euo pipefail

echo "=== Linux Installer (install.sh) Starting ==="

VENV_PATH="$HOME/.global_venv"

# 1. Create Python venv if missing
if [ ! -d "$VENV_PATH" ]; then
    echo "Creating Python virtual environment at $VENV_PATH..."
    python3 -m venv "$VENV_PATH"
fi

PYTHON_BIN="$VENV_PATH/bin/python3"
PIP_BIN="$VENV_PATH/bin/pip"

# 2. Install / Upgrade required Python packages
# (kept in sync with requirements.txt -- see that file for what each one is for)
echo "Installing required Python dependencies..."
"$PIP_BIN" install --quiet --upgrade pip
"$PIP_BIN" install --quiet -r "$(dirname "${BASH_SOURCE[0]}")/../requirements.txt"

echo "Python packages installed cleanly."

# 3. Environment Health Check
echo "Checking Linux system tools..."
# pdftoppm (from the 'poppler-utils' package) is required alongside soffice
# for the diagram-conversion and page-by-page visual-QA tools. Each tool's
# actual apt PACKAGE name isn't always the same as the command name (e.g.
# `pdftoppm` the command comes from the `poppler-utils` package), so this
# is a plain lookup table rather than trying to derive the package name
# from the command name.
declare -A APT_PACKAGE_FOR=( [node]="nodejs" [soffice]="libreoffice" [pdftoppm]="poppler-utils" )
for tool in node soffice pdftoppm; do
    if command -v "$tool" >/dev/null 2>&1; then
        echo "  [OK] Tool '$tool' is installed."
    else
        echo "  [WARNING] Tool '$tool' is NOT installed. Install via: sudo apt install ${APT_PACKAGE_FOR[$tool]}"
    fi
done

# The Node.js "docx" package is what the generator's build script uses to
# actually produce the .docx file (see templates/study-notes-skill.md's
# Formatting section). It's a global npm package, so requirements.txt
# (Python-only) can't list it -- install it explicitly here instead.
if command -v npm >/dev/null 2>&1; then
    if npm ls -g docx >/dev/null 2>&1; then
        echo "  [OK] Node package 'docx' is already installed globally."
    else
        echo "Installing Node 'docx' package globally..."
        npm install -g docx && echo "  [OK] Node package 'docx' installed." \
            || echo "  [WARNING] Failed to install the 'docx' npm package. Install manually: npm install -g docx"
    fi
else
    echo "  [WARNING] npm not found; cannot install the 'docx' package. Install Node.js first."
fi

# 4. Check API Env file
if [ -f "$HOME/.anthropic_env" ]; then
    echo "  [OK] API key environment file found (~/.anthropic_env)."
else
    echo "  [NOTICE] ~/.anthropic_env is missing. Create it with:"
    echo '           echo "ANTHROPIC_API_KEY=sk-ant-..." > ~/.anthropic_env && chmod 600 ~/.anthropic_env'
fi

# 5. Set up the passwordless Google Drive remount helper.
#
# WHY THIS IS NEEDED: the unattended nightly job sometimes finds /mnt/g in
# a "stale" state -- Google Drive for Desktop remounted G: on Windows, but
# WSL is still holding its old (now-dead) handle. The only fix is to
# remount it (`umount -l /mnt/g && mount -t drvfs G: /mnt/g`), and `mount`
# requires root. Since the nightly job runs unattended with nobody watching
# to type a password, it needs a NARROW, PASSWORDLESS sudo rule that allows
# running ONLY this one specific helper script -- nothing else.
#
# This step needs YOUR interactive sudo password once, right now -- that's
# expected, and different from the nightly job itself (which must never
# need one). It also directly edits sudo's own configuration, one of the
# most security-sensitive files on the system: a mistake there can break
# `sudo` entirely. So this is done carefully:
#   1. The sudoers RULE is written to a TEMP file first, never directly
#      into /etc/sudoers.d/.
#   2. That temp file's syntax is validated with `visudo -c -f` BEFORE it
#      ever touches the real sudoers directory -- an invalid rule is
#      rejected here and never installed.
#   3. Only after that check passes are the helper script and the
#      validated rule installed, via `sudo install` (which sets correct,
#      safe file permissions and ownership in the same step).
echo "Setting up passwordless Google Drive remount helper (requires sudo)..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOUNT_SRC="$SCRIPT_DIR/remount-gdrive"
REMOUNT_DST="/usr/local/sbin/remount-gdrive"
SUDOERS_FILE="/etc/sudoers.d/study-notes-remount"

if [ ! -f "$REMOUNT_SRC" ]; then
    echo "  [WARNING] $REMOUNT_SRC not found; skipping remount-helper setup."
elif ! command -v sudo >/dev/null 2>&1; then
    echo "  [WARNING] sudo not available; skipping remount-helper setup. See docs/setup-guide.md to set this up by hand."
else
    # This whole block is best-effort: a failure here must not abort the
    # rest of the installer (the pipeline still works without it -- it
    # just won't be able to self-heal a stale mount unattended).
    set +e

    sudo install -m 0755 -o root -g root "$REMOUNT_SRC" "$REMOUNT_DST"
    install_rc=$?

    sudoers_rc=1
    if [ "$install_rc" -eq 0 ]; then
        SUDOERS_TMP="$(mktemp)"
        echo "$USER ALL=(ALL) NOPASSWD: $REMOUNT_DST" > "$SUDOERS_TMP"
        if sudo visudo -c -f "$SUDOERS_TMP" >/dev/null 2>&1; then
            sudo install -m 0440 -o root -g root "$SUDOERS_TMP" "$SUDOERS_FILE"
            sudoers_rc=$?
        else
            echo "  [WARNING] Generated sudoers rule failed validation; NOT installing it. Sudo config left untouched."
        fi
        rm -f "$SUDOERS_TMP"
    fi

    set -e
    if [ "$install_rc" -eq 0 ] && [ "$sudoers_rc" -eq 0 ]; then
        # Non-invasive check: list what this user can run passwordlessly,
        # rather than actually EXECUTING the helper (which would remount
        # /mnt/g for real -- an unwanted side effect just to "verify" it).
        if sudo -n -l 2>/dev/null | grep -qF "$REMOUNT_DST"; then
            echo "  [OK] Passwordless remount helper installed and verified at $REMOUNT_DST."
        else
            echo "  [OK] Passwordless remount helper installed at $REMOUNT_DST (run 'sudo -l' to double check the rule is active)."
        fi
    else
        echo "  [WARNING] Could not fully set up the remount helper. See docs/setup-guide.md to do it by hand."
    fi
fi

echo "=== Linux Installer finished cleanly ==="

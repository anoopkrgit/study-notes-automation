#!/usr/bin/env bash
# wsl-study-notes-processor.sh
# WSL Linux processor entrypoint & mount helper

set -euo pipefail

LOCAL_ROOT="/mnt/c/06-PROJECTS/trial/study-notes-automation-redesigned"
LOG_DIR="$LOCAL_ROOT/logs"
LOG_FILE="$LOG_DIR/study-notes-pipeline.log"
PARENT="/mnt/g/My Drive/000-Education/00-Avyaan/Bakliwal-Study-9th-26-27/AI-Chapter-Notes"
PYTHON_BIN="$HOME/.global_venv/bin/python3"

[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"

# REPO_ROOT is one level ABOVE this script's own directory. The pipeline
# now runs directly from the dev repo (no more staging-copy into a fixed
# C:\StudyNotesAutomation folder, so code edits take effect immediately
# without re-running the installer) -- this script lives in <repo>/scripts/,
# while src/main.py (which main.py's own imports like `import settings as
# config` / `from src....` rely on) lives at <repo>/, one level up.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

mkdir -p "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

# Fail fast with a CLEAR message if the pipeline code isn't actually where
# this script expects it -- e.g. someone copied only this wrapper script
# without re-running the full installer. Better than a cryptic Python
# "file not found" traceback three log lines from now.
if [ ! -f "$REPO_ROOT/src/main.py" ]; then
  echo "$(date -Is)  [WSL-PROC]  ERROR: $REPO_ROOT/src/main.py not found."
  echo "$(date -Is)  [WSL-PROC]  Re-run install.bat to redeploy the pipeline code into $REPO_ROOT."
  exit 1
fi

echo "===================================================================="
echo "$(date -Is)  [WSL-PROC]  wsl-study-notes-processor starting..."

# 1. Remount Google Drive if /mnt/g is missing or stale
WAIT_TOTAL=60
waited=0
while [ ! -d "$PARENT" ] && [ "$waited" -lt "$WAIT_TOTAL" ]; do
  echo "$(date -Is)  [WSL-PROC]  Parent folder not found; running remount-gdrive helper..."
  sudo -n /usr/local/sbin/remount-gdrive >/dev/null 2>&1 || true
  [ -d "$PARENT" ] && break
  sleep 5
  waited=$((waited + 5))
done

if [ ! -d "$PARENT" ]; then
  echo "$(date -Is)  [WSL-PROC]  WARNING: Parent folder still not available: $PARENT"
fi

# 2. Check DUMMY_UNTIL timestamp gate
#
# Zero-token guarantee while dummy mode is active: BOTH stages run without
# the LLM (--run-assemble-no-llm, --run-generate-no-llm), and assembly is
# also --dry-run so it doesn't even write files -- only deterministic
# filename-based routing and logging happen, so this costs nothing no
# matter how it's invoked.
DUMMY_UNTIL="2026-07-25 00:00"
if [ -n "$DUMMY_UNTIL" ]; then
  cutoff_epoch="$(date -d "$DUMMY_UNTIL" +%s 2>/dev/null || echo 0)"
  if [ "$cutoff_epoch" -gt 0 ] && [ "$(date +%s)" -lt "$cutoff_epoch" ]; then
    echo "$(date -Is)  [WSL-PROC]  DUMMY MODE active (until $DUMMY_UNTIL local); running a zero-token dry pass only."
    "$PYTHON_BIN" "$REPO_ROOT/src/main.py" --run-assemble-no-llm --dry-run --run-generate-no-llm
    exit 0
  fi
fi

# 3. Execute Unified Python Engine
#
# Nightly token policy (deliberately explicit here as actual flags, not
# left to whatever main.py's own argparse defaults happen to be, so this
# behaviour is visible right where it actually runs and won't silently
# change if a default is ever edited):
#   --run-assemble        Stage 1 DOES use the LLM content router, so files
#                          actually get classified and filed correctly
#                          overnight.
#   --run-generate-no-llm Stage 2 runs WITHOUT the LLM: it still does
#                          everything except talk to the API (picks the
#                          next chapter, lists its input files, logs all of
#                          it) so you can see every morning exactly which
#                          chapter is queued up next -- but spends ZERO
#                          tokens, since nobody is watching to catch a bad
#                          multi-turn generation run overnight. Run
#                          `python3 src/main.py --run-generate` yourself,
#                          watching the log, when you're ready to actually
#                          generate a chapter's .docx for real.
echo "$(date -Is)  [WSL-PROC]  Executing main Python pipeline: --run-assemble --run-generate-no-llm  (assemble: LLM ON, spends tokens | generate: LLM OFF, zero tokens)..."
set +e
"$PYTHON_BIN" "$REPO_ROOT/src/main.py" --run-assemble --run-generate-no-llm
rc=$?
set -e

echo "$(date -Is)  [WSL-PROC]  Python pipeline finished with exit code $rc"
exit "$rc"

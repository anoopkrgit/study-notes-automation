#!/usr/bin/env bash
# wsl-study-notes-processor.sh
# WSL Linux processor entrypoint & mount helper

set -euo pipefail

# -h/--help is intercepted FIRST, before any of the setup work below
# (log-file redirect, Google Drive mount check/wait, DUMMY_UNTIL gate) --
# a real CLI's --help returns instantly and touches nothing, it doesn't
# wait up to 60s for a drive mount or silently vanish into a log file.
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
NAME
    wsl-study-notes-processor.sh -- WSL entrypoint for the Study Notes
    Automation Pipeline (file routing + note generation)

SYNOPSIS
    wsl-study-notes-processor.sh [-h|--help]
    wsl-study-notes-processor.sh [OPTIONS...]
    wsl-study-notes-processor.sh                (no args -> nightly default)

DESCRIPTION
    Windows Task Scheduler's nightly job launches this script inside WSL
    (see win-environment-setup.ps1). It heals a stale Google Drive mount,
    checks a DUMMY_UNTIL safety gate, then runs the pipeline's real Python
    CLI (src/main.py), forwarding every argument this script itself
    received ("$@") straight through, unmodified. Nothing here is
    hardcoded: which implementation runs, and how much it spends, is a
    pure CLI choice that flows all the way from the top -- Task
    Scheduler's -PipelineArgs, through win-environment-setup.ps1's
    -PipelineArgs parameter, through this script -- down to src/main.py,
    with zero file edits required at any layer.

    Invoked with NO arguments at all (the nightly Scheduled Task's
    default, since its registered action passes none), falls back to:
        --stage1-mode llm-full --stage2-mode no-llm

OPTIONS
    All options below are src/main.py's own flags, forwarded verbatim --
    this script defines none of its own. Run `python3 src/main.py --help`
    for the fully authoritative, always-current list; this is a curated
    summary organized around the question this script's own docs get
    asked most: "which of the three implementations does this apply to?"

    --stage1-mode {off,no-llm,llm-token-saver,llm-full}
    --stage2-mode {off,no-llm,llm-token-saver,llm-full}
        Whether/how each stage spends. Applies IDENTICALLY to whichever
        --stageN-impl is selected below -- all three implementations
        honor the same four states:
          off               don't run this stage at all      (default)
          no-llm            deterministic, zero LLM/CLI calls
          llm-token-saver   real call, near-zero cost, dummy content
          llm-full          real call, full cost, real output

    --stage1-impl {legacy,graph,subprocess}
    --stage2-impl {legacy,graph,subprocess}
        Which code path executes -- the THREE PARALLEL IMPLEMENTATIONS:

          legacy       src/direct_api/            direct Anthropic SDK calls
                         Stage 1: available.  Stage 2: available.
          graph        src/agents/                LangGraph multi-agent flowchart
                         Stage 1: available.  Stage 2: available.
          subprocess   src/claude_cli_subprocess/ headless `claude` CLI,
                         billed via Claude subscription, not the metered API key
                         Stage 1: available.  Stage 2: available.

    --doctor        Environment health check, then exit (ignores everything else)
    --dry-run       Stage 1 only: show planned actions, touch nothing
    --verbose       Enable verbose logging
    --quiet         Suppress INFO logs; show only warnings and errors

EXAMPLES
    wsl-study-notes-processor.sh
        Nightly default: assemble WITH the LLM (legacy), generate preview only.

    wsl-study-notes-processor.sh --stage1-mode llm-full --stage2-mode llm-full
        A full, real, paid run of both stages (legacy implementation, the default).

    wsl-study-notes-processor.sh --stage1-impl graph --stage2-impl graph \
        --stage1-mode llm-token-saver --stage2-mode llm-token-saver
        Cheap end-to-end smoke test of the agentic (graph) implementation.

    wsl-study-notes-processor.sh --stage1-impl subprocess --stage1-mode llm-full
        Real Stage 1 routing via the claude CLI subprocess (Stage 2 not run).

    wsl-study-notes-processor.sh --stage1-mode llm-full --stage1-impl subprocess \
        --stage2-mode llm-token-saver --stage2-impl graph
        Mixed: real Stage 1 via the CLI subprocess, cheap Stage 2 wiring
        smoke test via the graph implementation -- the two stages' modes
        and implementations are fully independent of each other.

NOTES
    Every OTHER invocation (any args not -h/--help) runs the pipeline for
    real: Google Drive mount check/heal, then src/main.py "$@" verbatim,
    with all output appended to the unified log file
    (config.UNIFIED_LOG_FILE), not printed to this terminal.
EOF
  exit 0
fi

# REPO_ROOT is one level ABOVE this script's own directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

PYTHON_BIN="$HOME/.global_venv/bin/python3"

[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"

# Extract configuration from settings.py dynamically to prevent hardcoded duplication
LOG_FILE=$("$PYTHON_BIN" -c "import sys; sys.path.insert(0, '$REPO_ROOT'); from config.settings import UNIFIED_LOG_FILE; print(UNIFIED_LOG_FILE)")
PARENT=$("$PYTHON_BIN" -c "import sys; sys.path.insert(0, '$REPO_ROOT'); from config.settings import DEFAULT_TARGET_ROOT; print(DEFAULT_TARGET_ROOT)")

mkdir -p "$(dirname "$LOG_FILE")"
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
# the LLM (--stage1-mode no-llm, --stage2-mode no-llm), and assembly is
# also --dry-run so it doesn't even write files -- only deterministic
# filename-based routing and logging happen, so this costs nothing no
# matter how it's invoked.
DUMMY_UNTIL="2026-07-25 00:00"
if [ -n "$DUMMY_UNTIL" ]; then
  cutoff_epoch="$(date -d "$DUMMY_UNTIL" +%s 2>/dev/null || echo 0)"
  if [ "$cutoff_epoch" -gt 0 ] && [ "$(date +%s)" -lt "$cutoff_epoch" ]; then
    echo "$(date -Is)  [WSL-PROC]  DUMMY MODE active (until $DUMMY_UNTIL local); running a zero-token dry pass only."
    "$PYTHON_BIN" "$REPO_ROOT/src/main.py" --stage1-mode no-llm --dry-run --stage2-mode no-llm
    exit 0
  fi
fi

# 3. Execute Unified Python Engine
#
# Which pipeline flags actually reach main.py is a pure CLI choice,
# forwarded verbatim from however THIS script itself was invoked ("$@") --
# never hardcoded here and never read from an env var, so choosing
# legacy/graph/subprocess (and cheap-smoke-test-vs-real generation) never
# requires editing this file. See `python3 src/main.py --help` for the
# full flag set, in particular --stage1-impl/--stage2-impl
# {legacy,graph,subprocess} and --stage1-mode/--stage2-mode
# {off,no-llm,llm-token-saver,llm-full}.
#
# If this script is invoked with NO arguments at all (e.g. the nightly
# Scheduled Task, whose registered action doesn't pass any), it falls back
# to the long-standing nightly policy:
#   --stage1-mode llm-full   Stage 1 DOES use the LLM content router at full
#                             cost, so files actually get classified and
#                             filed correctly overnight.
#   --stage2-mode no-llm     Stage 2 runs WITHOUT the LLM: it still does
#                             everything except talk to the API (picks the
#                             next chapter, lists its input files, logs all
#                             of it) so you can see every morning exactly
#                             which chapter is queued up next -- but spends
#                             ZERO tokens, since nobody is watching to catch
#                             a bad multi-turn generation run overnight.
if [ "$#" -eq 0 ]; then
  set -- --stage1-mode llm-full --stage2-mode no-llm
fi
echo "$(date -Is)  [WSL-PROC]  Executing main Python pipeline with args: $*"
set +e
"$PYTHON_BIN" "$REPO_ROOT/src/main.py" "$@"
rc=$?
set -e

echo "$(date -Is)  [WSL-PROC]  Python pipeline finished with exit code $rc"
exit "$rc"

# Configuration file for Study Notes Automation Pipeline
import os
from pathlib import Path

# Base Paths — auto-detected from this file's location (config/settings.py → project root)
# Works from any clone location on any machine, no manual editing required.
LOCAL_RUNTIME_ROOT = Path(__file__).resolve().parent.parent

LOG_DIR = LOCAL_RUNTIME_ROOT / "logs"
STATE_DIR = LOCAL_RUNTIME_ROOT / "state"

UNIFIED_LOG_FILE = LOG_DIR / "study-notes-pipeline.log"
STATE_FILE = STATE_DIR / "assemble-state.json"
RETRY_EPOCH_FILE = STATE_DIR / "retry-epoch.txt"
CHAPTER_PROGRESS_DIR = STATE_DIR / "progress"

# Default Target Folders (Can be overridden by env vars)
DEFAULT_TRANSCRIPT_SRC = Path(os.environ.get("TRANSCRIPT_SRC", "/mnt/g/My Drive/Education/Student/Lecture-Downloads"))
DEFAULT_COLLECTED_SRC  = Path(os.environ.get("COLLECTED_SRC",  "/mnt/g/My Drive/Education/Student/Study-Programme/Collected-Study-Materials"))
DEFAULT_TARGET_ROOT    = Path(os.environ.get("TARGET_ROOT",    "/mnt/g/My Drive/Education/Student/Study-Programme/AI-Chapter-Notes"))

# Tunables
IDLE_DAYS = 14
CONF_MIN = 0.55
DISAGREE_MIN = 0.80
SNIPPET_CHARS = 6000
SNIPPET_PAGES = 3
# How many separate PROCESS RUNS (e.g. across nights, if the model keeps
# hitting a retryable API error) one chapter's generation may need before
# the pipeline gives up on it and writes a failure marker for a human.
MAX_ATTEMPTS = 5
# Safety valve on how many back-and-forth turns (one turn = one model
# response, which may include several tool calls) a SINGLE generation
# attempt may take, WITHIN one process run, before that run gives up. This
# is deliberately generous -- a full illustrated chapter document
# realistically needs many dozens of tool calls (writing sections, drawing
# diagrams, converting them, checking each one, assembling and proofreading
# the .docx page by page). This number exists only to catch a genuinely
# stuck/looping run, not to cap normal work.
MAX_TURNS = 200

# Models
ROUTER_MODEL = os.environ.get("ASSEMBLE_ROUTER_MODEL", "claude-haiku-4-5-20251001")
GENERATOR_MODEL = os.environ.get("STUDY_NOTES_MODEL", "claude-sonnet-5")

# Special File Markers
HOLD = "_hold"
MARKER = "_notes_done"
FAILMARK = "_notes_FAILED.txt"
SOURCES = "_sources.txt"
NEWMAT = "_new-material.txt"
SUP_DIR = "supporting"
TRANSCRIPTS_DIR = "transcripts"
REVIEW_DIR_NAME = "_needs_review"

IGNORE_NAMES = {"desktop.ini", "Thumbs.db", ".DS_Store"}
IGNORE_EXTS = {".tmp", ".part", ".crdownload"}

# ---------------------------------------------------------------------------
# Multi-Agent Architecture settings (src/agents/)
# All default to legacy behavior — zero change until explicitly opted in.
# ---------------------------------------------------------------------------

# Per-agent model overrides
AUTHOR_MODEL   = os.environ.get("STUDY_NOTES_AUTHOR_MODEL", "claude-sonnet-5")
FIGURE_MODEL   = os.environ.get("STUDY_NOTES_FIGURE_MODEL", "claude-haiku-4-5")
COMPILER_MODEL = os.environ.get("STUDY_NOTES_COMPILER_MODEL", "claude-haiku-4-5")  # error-diagnosis only
TRIAGE_MODEL   = os.environ.get("ASSEMBLE_ROUTER_MODEL", "claude-haiku-4-5-20251001")  # compat alias
GENERATOR_MODEL_ALIAS = AUTHOR_MODEL  # back-compat alias

# QA↔Author retry cap (replaces the implicit "loop until turn 200" safety valve
# with an explicit, targeted retry cap on the one edge that actually needs it)
QA_MAX_RETRY_LOOPS = int(os.environ.get("STUDY_NOTES_QA_MAX_RETRIES", "5"))

# LangGraph checkpointer storage
GRAPH_CHECKPOINT_DIR = STATE_DIR / "graph-checkpoints"

# Rollout wrapper (src/agents/dispatch.py reads these)
# "legacy" = existing monolithic code, zero behavior change
# "graph"  = new multi-agent LangGraph implementation
STAGE1_IMPL = os.environ.get("STUDY_NOTES_STAGE1_IMPL", "legacy")
STAGE2_IMPL = os.environ.get("STUDY_NOTES_STAGE2_IMPL", "legacy")

# Cost-safe dev/test toggle — forces cheap models, tiny prompts, capped output,
# and dummy inputs across all graph nodes.  NEVER enable for production runs.
DEV_TOKEN_SAVER_MODE = os.environ.get("DEV_TOKEN_SAVER_MODE", "0") == "1"

# ---------------------------------------------------------------------------
# claude CLI settings (src/claude_cli_subprocess/) -- the "subprocess" implementation.
# Invokes the `claude` binary as a subprocess (Claude-subscription billing)
# rather than calling the Anthropic SDK directly. Defaults work with a bare
# `claude` on PATH -- zero config needed to try it. CLAUDE_BIN is shared by
# both Stage 1 and Stage 2 (see cli-subprocess-plan.md).
# ---------------------------------------------------------------------------
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# Reuses the same ASSEMBLE_ROUTER_MODEL env var as ROUTER_MODEL above -- one
# router-model setting shared across whichever --stage1-impl is selected.
ESCALATE_MODEL = os.environ.get("ASSEMBLE_ESCALATE_MODEL", "claude-sonnet-5")

# DEV_TOKEN_SAVER_MODE (see the rollout section above) applied to `claude`
# CLI subprocess calls: the CLI has no `max_tokens` SDK parameter to cap,
# so these two native CLI flags are the cost-safety equivalent --
# `--max-budget-usd` is a hard dollar ceiling per invocation, `--effort low`
# asks the model itself to do less work. Applied together with a dummy
# prompt and skipped escalation (see src/claude_cli_subprocess/stage1_cli.py).
#
# 0.10, not 0.02: confirmed live (Stage 2 pilot verification) that a
# single dev-mode `claude -p` session has a cost floor somewhere above
# $0.02 regardless of tool grants or prompt brevity -- two real calls hit
# `error_max_budget_usd` at $0.145 (full production --allowedTools) and
# $0.048 (empty --allowedTools) respectively, with input/output_tokens
# both reported as 0 in the usage object either way (the actual cost
# driver isn't itemized there). At $0.02 the budget check fires on every
# single dev-mode call before the session can finish, meaning the
# "success" code path of DEV_TOKEN_SAVER_MODE was never actually
# reachable -- only the failure/retry path ever got exercised, defeating
# the point of a wiring smoke test. $0.10 sits comfortably above the
# observed floor while remaining a tiny fraction of a real generation
# run's cost.
DEV_TOKEN_SAVER_MAX_BUDGET_USD = os.environ.get("DEV_TOKEN_SAVER_MAX_BUDGET_USD", "0.10")
DEV_TOKEN_SAVER_EFFORT = os.environ.get("DEV_TOKEN_SAVER_EFFORT", "low")

# ---------------------------------------------------------------------------
# claude CLI settings for Stage 2 (src/claude_cli_subprocess/stage2_cli.py).
# Additive only -- GENERATOR_MODEL/MAX_TURNS/MAX_ATTEMPTS above stay
# untouched; those belong to direct_api/ and agents/, not this module.
# ---------------------------------------------------------------------------
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")  # unset by default; when set, passed as --model
CLAUDE_ALLOWED_TOOLS = os.environ.get("CLAUDE_ALLOWED_TOOLS",
    "Bash(python3 *),Bash(node *),Bash(npm *),Bash(pdftotext *),Bash(pdfinfo *),"
    "Bash(pdftoppm *),Bash(soffice *),Read,Write,Edit,Glob,Grep")
CLAUDE_PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
CLAUDE_CLI_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_CLI_TIMEOUT_SECONDS", "3600"))
CLAUDE_RETRY_EPOCH_DEFAULT_SECONDS = int(os.environ.get("CLAUDE_RETRY_EPOCH_DEFAULT_SECONDS", str(5 * 3600)))
CLAUDE_SKILL_SOURCE_GLOB = os.environ.get("CLAUDE_SKILL_SOURCE_GLOB", "templates/*.skill")
CLAUDE_SKILL_INSTALL_DIR = LOCAL_RUNTIME_ROOT / ".claude" / "skills" / "study-notes"
CLAUDE_WORKSPACE_ROOT = LOCAL_RUNTIME_ROOT / "generation-workspace"  # per-chapter scratch subfolders

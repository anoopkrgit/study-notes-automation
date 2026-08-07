# Configuration file for Study Notes Automation Pipeline
import os
import platform
from pathlib import Path

# Base Paths — auto-detected from this file's location (config/settings.py → project root)
# Works from any clone location on any machine, no manual editing required.
# Also works unmodified when installed as a pip dependency (e.g. by
# run-claude-agent), because config/, src/, and templates/ are installed as
# siblings under one root (see pyproject.toml's packaging note) -- so this
# still resolves to the right place without needing the override below.
#
# STUDY_NOTES_RUNTIME_ROOT overrides this for callers that want
# logs/state/generation-workspace written somewhere else entirely (e.g. a
# user-writable dir instead of inside site-packages). Note this ALSO moves
# where the skill zip is expected (CLAUDE_SKILL_SOURCE_GLOB is resolved
# relative to this same root) -- only set it if templates/*.skill actually
# exists at the new location too.
LOCAL_RUNTIME_ROOT = (
    Path(os.environ["STUDY_NOTES_RUNTIME_ROOT"])
    if os.environ.get("STUDY_NOTES_RUNTIME_ROOT")
    else Path(__file__).resolve().parent.parent
)

LOG_DIR = LOCAL_RUNTIME_ROOT / "logs"
STATE_DIR = LOCAL_RUNTIME_ROOT / "state"

UNIFIED_LOG_FILE = LOG_DIR / "study-notes-pipeline.log"
STATE_FILE = STATE_DIR / "assemble-state.json"
RETRY_EPOCH_FILE = STATE_DIR / "retry-epoch.txt"
CHAPTER_PROGRESS_DIR = STATE_DIR / "progress"

# Default Target Folders (Can be overridden by env vars)
# These defaults only apply to users running study-notes-automation DIRECTLY.
# When driven by the runner (run-claude-agent), paths_config.py exports
# TRANSCRIPT_SRC/COLLECTED_SRC/TARGET_ROOT first, so the env-var branch wins
# and these literals are never used. The Google Drive mount lives at /mnt/g on
# WSL/Linux but at the G: drive on native Windows, so branch the default root
# per-OS -- otherwise a direct Windows user gets an unusable /mnt/g path.
_GDRIVE_ROOT = "G:/My Drive" if platform.system() == "Windows" else "/mnt/g/My Drive"
_EDU_ROOT = f"{_GDRIVE_ROOT}/Education/Student"

DEFAULT_TRANSCRIPT_SRC = Path(os.environ.get("TRANSCRIPT_SRC", f"{_EDU_ROOT}/Lecture-Downloads"))
DEFAULT_COLLECTED_SRC  = Path(os.environ.get("COLLECTED_SRC",  f"{_EDU_ROOT}/Study-Programme/Collected-Study-Materials"))
DEFAULT_TARGET_ROOT    = Path(os.environ.get("TARGET_ROOT",    f"{_EDU_ROOT}/Study-Programme/AI-Chapter-Notes"))

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
WEB_SOURCES = "_web-sources.txt"
RETRO_FINDINGS = "_retro-findings.txt"
RUN_DIAGNOSTICS = "_run-diagnostics.txt"  # per-run efficiency telemetry mined from the
    # claude CLI session transcript (ad-hoc scratch scripts written vs build.js --patch
    # invocations) -- see write_run_diagnostics() in claude_cli_subprocess/stage2_cli.py
    # and docs/stage2-token-burn-postmortem.md for the pattern it exists to make visible.
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
# 0.20, not 0.02: confirmed live (Stage 2 pilot verification) that a
# single dev-mode `claude -p` session has a cost floor somewhere above
# $0.02 regardless of tool grants or prompt brevity -- real calls have hit
# `error_max_budget_usd` at $0.048 (empty --allowedTools), $0.145 (full
# production --allowedTools) and $0.176 (native-Windows first run), with
# input/output_tokens both reported as 0 in the usage object either way
# (the actual cost driver isn't itemized there). At $0.02 the budget check
# fires on every single dev-mode call before the session can finish,
# meaning the "success" code path of DEV_TOKEN_SAVER_MODE was never
# actually reachable -- only the failure/retry path ever got exercised,
# defeating the point of a wiring smoke test. $0.20 sits above the highest
# observed floor (~$0.176) while remaining a tiny fraction of a real
# generation run's cost. Override with DEV_TOKEN_SAVER_MAX_BUDGET_USD.
DEV_TOKEN_SAVER_MAX_BUDGET_USD = os.environ.get("DEV_TOKEN_SAVER_MAX_BUDGET_USD", "0.20")
DEV_TOKEN_SAVER_EFFORT = os.environ.get("DEV_TOKEN_SAVER_EFFORT", "low")

# ---------------------------------------------------------------------------
# claude CLI settings for Stage 2 (src/claude_cli_subprocess/stage2_cli.py).
# Additive only -- GENERATOR_MODEL/MAX_TURNS/MAX_ATTEMPTS above stay
# untouched; those belong to direct_api/ and agents/, not this module.
# ---------------------------------------------------------------------------
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")  # pinned rather than left
    # unset: an unset value means run_claude_cli() never passes --model at all, so the
    # `claude` CLI's own current default silently decides -- confirmed live that was
    # claude-sonnet-5, which is what's pinned here now (no behavior change today), so
    # future runs can't silently change model if that CLI-side default ever drifts.
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "")  # unset by default; when set (low/medium/high/xhigh/max),
                                                       # passed as --effort on LIVE (non-dev-mode) calls only --
                                                       # dev-mode's own DEV_TOKEN_SAVER_EFFORT above is separate
                                                       # and always wins while dev mode is on.
CLAUDE_ALLOWED_TOOLS = os.environ.get("CLAUDE_ALLOWED_TOOLS",
    "Bash(python3 *),Bash(node *),Bash(npm *),Bash(pdftotext *),Bash(pdfinfo *),"
    "Bash(pdftoppm *),Bash(soffice *),"
    # Read-only/scratch shell verbs the skill's own pipeline needs. Added after a
    # live run burned ~68 turns purely on "this command requires approval"
    # denials that no one was there to approve (unattended run) -- see
    # docs/stage2-token-burn-postmortem.md. NOT a blanket Bash grant: each verb
    # is still individually scoped, and `export` is deliberately still absent
    # (NODE_PATH/PYTHONIOENCODING are set process-side in
    # claude_cli_subprocess/common.py's build_claude_env() instead).
    "Bash(ls *),Bash(grep *),Bash(cat *),Bash(wc *),Bash(mkdir *),"
    "Bash(rm *),Bash(env *),Bash(cd *),"
    "Read,Write,Edit,Glob,Grep")
CLAUDE_PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
CLAUDE_CLI_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_CLI_TIMEOUT_SECONDS", "3600"))
CLAUDE_CLI_HEARTBEAT_SECONDS = int(os.environ.get("CLAUDE_CLI_HEARTBEAT_SECONDS", "120"))  # how
    # often (seconds) a one-line "still working, currently doing X" heartbeat is written to the
    # unified pipeline log while a live claude CLI call is in progress (the call itself is a single
    # blocking subprocess call, so without this the log goes silent for up to CLAUDE_CLI_TIMEOUT_SECONDS)
CLAUDE_RETRY_EPOCH_DEFAULT_SECONDS = int(os.environ.get("CLAUDE_RETRY_EPOCH_DEFAULT_SECONDS", str(5 * 3600)))
CLAUDE_SKILL_SOURCE_GLOB = os.environ.get("CLAUDE_SKILL_SOURCE_GLOB", "templates/*.skill")
CLAUDE_SKILL_INSTALL_DIR = LOCAL_RUNTIME_ROOT / ".claude" / "skills" / "study-notes"
# Global (user-level) install target, IN ADDITION TO the project-local one above, not
# instead of it. Belt-and-suspenders: project-local skill discovery via cwd walk-up
# (from run_claude_cli()'s nested generation-workspace/<chapter> cwd) was flagged as an
# unverified pilot item in docs/cli-subprocess-plan.md and confirmed NOT reliable on the
# first live run -- root-caused to this repo's .claude/ being gitignored, which likely
# makes it invisible to Claude Code's directory-walk skill discovery. Without an exact
# "study-notes" match anywhere visible, /study-notes silently fuzzy-matched a stale
# global skill instead (see sync_skill_package()'s docstring). Both locations are now
# kept in lockstep on every run so neither can ever go stale relative to
# templates/study-notes.skill, and global alone is enough for discovery regardless of
# cwd since personal-scope skills are always scanned.
CLAUDE_SKILL_GLOBAL_INSTALL_DIR = Path.home() / ".claude" / "skills" / "study-notes"
CLAUDE_WORKSPACE_ROOT = STATE_DIR / "workspace"  # per-chapter scratch subfolders

# ---------------------------------------------------------------------------
# Bounded web enrichment for Stage 2 (docs/web-enrichment-plan.md). Now ON by
# default. It originally shipped off ("0") pending validation against real
# --live runs, which meant it was never actually exercised: a real live run's
# closing summary claimed "web research was not used -- the transcripts were
# sufficient for this chapter's scope", when in fact WebSearch/WebFetch had
# never been granted at all (see docs/stage2-token-burn-postmortem.md). Still
# fully env-overridable (ENABLE_WEB_ENRICHMENT=0) and still bounded by
# WEB_SEARCH_ALLOWED_DOMAINS + MAX_WEB_SEARCHES_PER_CHAPTER below. Whichever
# way it resolves, the outcome is now RECORDED in config.WEB_SOURCES on every
# run rather than inferred from silence -- see write_web_sources_manifest() in
# src/claude_cli_subprocess/stage2_cli.py. Only wired into that module -- see
# the plan doc for why the guardrail mechanism (WebFetch domain-scoped
# permission rules + CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION) is CLI-specific
# and doesn't carry over to src/direct_api/ or src/agents/ as-is.
# ---------------------------------------------------------------------------
ENABLE_WEB_ENRICHMENT = os.environ.get("ENABLE_WEB_ENRICHMENT", "1") == "1"
WEB_SEARCH_ALLOWED_DOMAINS = [
    "ncert.nic.in",
    "hyperphysics.phy-astr.gsu.edu",
    "chem.libretexts.org",
    "khanacademy.org",
    "byjus.com",
]
MAX_WEB_SEARCHES_PER_CHAPTER = int(os.environ.get("MAX_WEB_SEARCHES_PER_CHAPTER", "3"))

# ---------------------------------------------------------------------------
# Automatic skill self-improvement (src/claude_cli_subprocess/stage2_cli.py's
# apply_retro_fixes()). Opt-in, off by default -- same rollout posture as
# ENABLE_WEB_ENRICHMENT above: real cost/risk (a second claude -p session per
# chapter that has retro candidates, autonomously editing the shared skill),
# so it doesn't start silently just because this config module got updated.
# Explicitly authorized to skip human approval (git history is the safety
# net) -- but tools/regress.py is still the automated accept/reject gate,
# independently re-run by OUR OWN code after the session claims success,
# never just trusted (this module's TRUTH-CHECK, NOT SELF-REPORTED SUCCESS
# rule applies here too). A failed gate discards the attempt; nothing about
# templates/study-notes.skill changes.
# ---------------------------------------------------------------------------
ENABLE_AUTO_SKILL_IMPROVEMENT = os.environ.get("ENABLE_AUTO_SKILL_IMPROVEMENT", "0") == "1"
AUTO_SKILL_IMPROVEMENT_WORKSPACE = CLAUDE_WORKSPACE_ROOT / "_skill_improvement"
AUTO_SKILL_IMPROVEMENT_TIMEOUT_SECONDS = int(os.environ.get("AUTO_SKILL_IMPROVEMENT_TIMEOUT_SECONDS", "1800"))

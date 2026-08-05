"""
stage2_api.py

Stage 2 Generator module: an "agentic" loop that talks to the Claude API
directly (no `claude` CLI, no Claude Code subscription -- see the project
README for why) to produce one chapter's study-notes .docx.

WHAT "AGENTIC LOOP" MEANS (for readers new to this pattern)
------------------------------------------------------------------
A single API call to Claude can't "go write a file" or "run a program" by
itself -- an API call is just: send some text in, get some text back. To
let Claude actually create files, draw diagrams, and run the docx-building
script, we give it a fixed menu of TOOLS (Python functions on our side,
listed in AGENT_TOOLS below) it's allowed to ask for. Each round trip
("turn") works like this:

  1. We send Claude the conversation so far.
  2. Claude replies with either: some text, and/or a request to call one or
     more of our tools (e.g. "call tool_write with these arguments").
  3. WE (this Python code, never Claude directly) actually run those tools
     and capture their results.
  4. We send the results back to Claude as the next turn, and repeat.

This continues until Claude replies with NO tool requests at all -- that's
its signal that it believes the work is done. We then check that the
expected .docx file genuinely exists before trusting that signal.

Defaults to MOCK MODE (a token-saving metadata-only ping) unless --live is
given, so that testing the pipeline's plumbing never accidentally spends
real API credits on a full generation run.

WHERE THIS FITS IN THE PROJECT
-------------------------------
- src/main.py calls run_generate() below once per pipeline run.
- Stage 1 (see src/direct_api/stage1_api.py) must run first: it sorts raw
  files into per-chapter folders under config.DEFAULT_TARGET_ROOT. This
  file only reads from those already-sorted folders -- it never looks at
  the original inbox folders itself.
- config/settings.py supplies every folder path, filename convention, and
  cost-control knob used here.
- src/func_tools_and_utils.py supplies the actual TOOL implementations
  (reading/writing files, running a shell command, etc.) that this file's
  AGENT_TOOLS menu below exposes to Claude, plus shared error-handling
  helpers.
- templates/study-notes.skill (a ZIP archive) holds the detailed
  content/formatting rulebook this file loads and hands to Claude as its
  main job instructions -- see load_skill_prompt() below.

INPUT / OUTPUT SUMMARY
------------------------
  IN:  one chapter folder already assembled by Stage 1 -- its class
       transcripts and any supporting material.
  OUT: a finished, print-ready study-notes Word document (.docx) saved
       into that same folder, plus a marker file recording success or
       failure so re-running the pipeline knows not to redo (or knows to
       retry) this chapter.
"""

import argparse
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import settings as config
from src.func_tools_and_utils import (
    logger, is_ignorable, classify_api_error, write_retry_epoch,
    tool_read, tool_write, tool_edit, tool_glob, tool_grep, tool_bash,
    tool_convert_to_png, tool_view_image, tool_view_pdf_page, _text_block,
    TokenTracker, EXIT_OK, EXIT_RATE_LIMITED, EXIT_FATAL
)
# Bounded web enrichment + autonomous retro, shared with src/agents and
# (for retro) src/claude_cli_subprocess -- see src/common/. Same capabilities
# PR #5/#7 added to the other two Stage 2 generators, ported here for parity.
from src.common.web_enrichment import web_search, web_fetch, write_web_sources_manifest
from src.common.skill_retro import capture_retro_findings, apply_retro_fixes
from src.claude_cli_subprocess.stage2_cli import sync_skill_package, _chapter_workspace

try:
    import anthropic
    from dotenv import load_dotenv
    load_dotenv(os.path.expanduser("~/.anthropic_env"))
    client = anthropic.Anthropic()
except Exception:
    client = None

def load_skill_prompt() -> str:
    """Load the study-notes skill text (the detailed content/formatting/
    accuracy rules the generated .docx must follow) to inject as the system
    prompt. If the file is somehow missing, fall back to one line rather
    than crashing -- generation would still attempt something, just with
    far less guidance.

    The skill ships as templates/study-notes.skill, a ZIP archive containing
    SKILL.md plus its tools/lib/schema -- not a flat markdown file. (Prior to
    this fix, this function pointed at a flat templates/study-notes-skill.md
    that never existed, so every --live run silently fell back to the
    one-line prompt below instead of the real skill definition.)"""
    zip_path = Path(__file__).resolve().parent.parent / "templates" / "study-notes.skill"
    if zip_path.exists():
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                if "SKILL.md" in zf.namelist():
                    return zf.read("SKILL.md").decode("utf-8")
                logger.warning(f"SKILL.md not found inside {zip_path}; using a minimal fallback prompt.")
        except Exception as e:
            logger.warning(f"Could not read skill archive {zip_path} ({e}); using a minimal fallback prompt.")
    else:
        logger.warning(f"Skill archive missing at {zip_path}; using a minimal fallback prompt.")
    return "Generate print-ready .docx study notes for the chapter."

def select_target_chapter(target_root: Path) -> Path:
    """Pick the first chapter folder that's ready for generation: has real
    input files, no .docx yet, and isn't marked done/failed/held.

    IMPORTANT: dot-prefixed folders (".claude", ".git", etc.) must be
    skipped explicitly. The original bash-based picker got this for free
    -- bash's default filename globbing (`for d in "$PARENT"/*/`) silently
    skips names starting with "." unless a shell option changes that.
    Path.iterdir() has no such default: it lists EVERY entry, dotfolders
    included. Caught by actually running the deployed pipeline end to end:
    it picked ".claude" (Claude Code's own settings folder) as a "chapter"
    and was about to try generating study notes from settings.local.json.
    """
    if not target_root.exists():
        return None
    for d in sorted(target_root.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name in ("Setup", config.REVIEW_DIR_NAME):
            continue
        if any(d.glob("*.docx")):
            continue
        if (d / config.MARKER).exists() or (d / config.FAILMARK).exists() or (d / config.HOLD).exists():
            continue
        transcripts_dir = d / config.TRANSCRIPTS_DIR
        inputs = [f for f in transcripts_dir.glob("*") if f.is_file() and not is_ignorable(f)] if transcripts_dir.exists() else []
        if inputs:
            return d
    return None

def build_user_prompt(target_dir: Path, top_transcripts: list, sup_files: list, expected_docx: Path) -> str:
    """The per-run instructions: which folder, which files play which role,
    and what to do. This is separate from the skill text (which defines
    universal content/formatting standards) -- this part is specific to
    THIS chapter's folder layout, the same way the original CLI-based
    pipeline's prompt explained the folder to the model on every run."""
    transcript_lines = "\n".join(f"  - {f.name}" for f in top_transcripts) or "  (none found)"
    supporting_lines = "\n".join(f"  - {f.name}" for f in sup_files) or "  (none)"
    # Bounded web enrichment (off unless config.ENABLE_WEB_ENRICHMENT) -- same
    # intent as the paragraph author_system_prompt() adds in src/agents. The
    # hard domain/budget limits are enforced in Python regardless of this text.
    web_note = ""
    if getattr(config, "ENABLE_WEB_ENRICHMENT", False):
        web_note = (
            "\nYou MAY use tool_web_search and tool_web_fetch to gather additional context, "
            "definitions, or reference material from the allowed domains only. Use this "
            "sparingly and strictly to enrich (never to expand scope beyond the transcripts). "
            "Do not fetch unnecessary pages.\n"
        )
    return f"""You are running fully unattended. Use the study-notes skill (given to you as
your system prompt) to generate ONE chapter's study notes.
{web_note}

The input materials for this chapter are in this folder:
  {target_dir}

Files under "{config.TRANSCRIPTS_DIR}/" are the class lecture transcripts -- these are the SPINE and
define the chapter's scope. Completeness against them is non-negotiable:
{transcript_lines}

Files under "{config.SUP_DIR}/" (if any) are extra reference material (revision notes,
worksheets, DPPs, answer keys). Use them to enrich explanations, examples and practice
problems, but they must NOT expand scope beyond what the transcripts cover:
{supporting_lines}

Ignore any "{config.SOURCES}" manifest file, "_prev/" (old versions), "{config.NEWMAT}",
"{config.HOLD}", and OS/cloud-sync metadata files (desktop.ini, Thumbs.db, .DS_Store).

Do the following without asking any questions:
1. Read every transcript and every supporting file per the roles above (tool_read for
   text; for PDFs, convert to images with tool_bash + pdftoppm and view them with
   tool_view_image, or use tool_view_pdf_page directly).
2. Infer the subject (Physics / Chemistry / Maths) and the chapter name from the contents.
3. Generate the complete print-ready study-notes .docx exactly as the skill specifies,
   including every diagram-QA and page-by-page accuracy check the skill's Accuracy and
   Pre-delivery Checklist sections require.
4. Save the final .docx to exactly this path: {expected_docx}
5. Once you have visually verified the finished document (per the skill's accuracy
   checklist), stop calling tools -- that is how the pipeline knows you are done.

Make reasonable assumptions where inputs are ambiguous and proceed to completion. Never
pause for confirmation."""

# AGENT_TOOLS is the fixed MENU of actions Claude is allowed to request
# during generation -- described in the same structured "tool" format used
# by ROUTER_TOOL in stage1_api.py (see that file for a fuller explanation
# of what a "tool" is in an AI API). Every entry below has a name, a
# plain-English description Claude reads to know when to use it, and an
# input_schema listing exactly what arguments it takes. Claude can never
# do anything OUTSIDE this menu -- e.g. it cannot delete a file, browse
# the internet, or run an arbitrary shell pipeline, because no such tool
# is offered here. execute_tool() further below is what actually RUNS
# whichever tool Claude asks for.
AGENT_TOOLS = [
    {
        "name": "tool_read",
        "description": "Read the contents of a TEXT file (.txt, .md, source code, etc.). For images or PDF pages, use tool_view_image / tool_view_pdf_page instead -- this tool cannot display pictures.",
        "input_schema": {
            "type": "object", "required": ["path"],
            "properties": {"path": {"type": "string"}}
        }
    },
    {
        "name": "tool_write",
        "description": "Write content to a file (creates it if it doesn't exist, overwrites if it does).",
        "input_schema": {
            "type": "object", "required": ["path", "content"],
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}}
        }
    },
    {
        "name": "tool_edit",
        "description": "Edit a file by replacing one exact occurrence of old_string with new_string.",
        "input_schema": {
            "type": "object", "required": ["path", "old_string", "new_string"],
            "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}
        }
    },
    {
        "name": "tool_glob",
        "description": "Find files matching a glob pattern (e.g. '*.svg') under a base directory.",
        "input_schema": {
            "type": "object", "required": ["pattern"],
            "properties": {"pattern": {"type": "string"}, "base_dir": {"type": "string"}}
        }
    },
    {
        "name": "tool_grep",
        "description": "Search for a regular expression pattern inside one text file.",
        "input_schema": {
            "type": "object", "required": ["pattern", "file_path"],
            "properties": {"pattern": {"type": "string"}, "file_path": {"type": "string"}}
        }
    },
    {
        "name": "tool_bash",
        "description": (
            "Execute ONE restricted, single command -- no pipes/chaining/redirection "
            "(python3, python, node, soffice, mkdir, ls, cat, cp, mv only). For the "
            "SVG-to-PNG diagram conversion pipeline, use tool_convert_to_png instead of "
            "trying to chain soffice and pdftoppm here -- that will be rejected."
        ),
        "input_schema": {
            "type": "object", "required": ["command"],
            "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}}
        }
    },
    {
        "name": "tool_convert_to_png",
        "description": "Convert a matplotlib-generated SVG diagram to a PNG file saved next to it (via soffice + pdftoppm internally). Use this instead of tool_bash for diagram conversion.",
        "input_schema": {
            "type": "object", "required": ["svg_path"],
            "properties": {"svg_path": {"type": "string"}, "dpi": {"type": "integer"}}
        }
    },
    {
        "name": "tool_view_image",
        "description": "View an image file (.png/.jpg/.jpeg/.webp) -- returns the actual picture so you can visually check it (diagram QA: arrowheads present, labels not overlapping, etc.).",
        "input_schema": {
            "type": "object", "required": ["path"],
            "properties": {"path": {"type": "string"}}
        }
    },
    {
        "name": "tool_view_pdf_page",
        "description": "Render and view ONE page of a PDF as an image, for the page-by-page accuracy proofread. Page numbers start at 1.",
        "input_schema": {
            "type": "object", "required": ["path", "page"],
            "properties": {"path": {"type": "string"}, "page": {"type": "integer"}}
        }
    },
]

# WEB_TOOLS is the OPTIONAL bounded-web-enrichment menu, appended to
# AGENT_TOOLS in run_generate() only when config.ENABLE_WEB_ENRICHMENT is on
# (off by default). Same capability the agents/ and subprocess/ generators
# have; the actual behavior lives in src/common/web_enrichment.py. Unlike the
# agents variant, these schemas take no chapter_dir (this loop doesn't inject
# one). Domain whitelisting and the per-chapter budget are enforced in Python
# (execute_tool / run_generate's loop), not trusted to the model.
WEB_TOOLS = [
    {
        "name": "tool_web_search",
        "description": (
            "Search the web for educational reference material, restricted to a whitelist of "
            "trusted domains. You MUST pass at least one allowed domain. Allowed: "
            + ", ".join(config.WEB_SEARCH_ALLOWED_DOMAINS)
        ),
        "input_schema": {
            "type": "object", "required": ["query", "allowed_domains"],
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "allowed_domains": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Domains to restrict the search to (must be from the allowed list above)."
                },
            }
        }
    },
    {
        "name": "tool_web_fetch",
        "description": "Fetch the readable text content of a specific URL returned by tool_web_search (must be on an allowed domain).",
        "input_schema": {
            "type": "object", "required": ["url"],
            "properties": {"url": {"type": "string", "description": "The URL to fetch"}}
        }
    },
]

def execute_tool(name: str, args: dict) -> list:
    """Run one tool call and return its result as a list of Anthropic
    content blocks. Almost every tool returns one text block; tool_view_image
    and tool_view_pdf_page return an actual image block instead, so the
    model can SEE the picture rather than just read a description of it."""
    try:
        if name == "tool_read": return _text_block(tool_read(args["path"]))
        if name == "tool_write": return _text_block(tool_write(args["path"], args["content"]))
        if name == "tool_edit": return _text_block(tool_edit(args["path"], args["old_string"], args["new_string"]))
        if name == "tool_glob": return _text_block(tool_glob(args["pattern"], args.get("base_dir", ".")))
        if name == "tool_grep": return _text_block(tool_grep(args["pattern"], args["file_path"]))
        if name == "tool_bash": return _text_block(tool_bash(args["command"], args.get("cwd", ".")))
        if name == "tool_convert_to_png": return _text_block(tool_convert_to_png(args["svg_path"], args.get("dpi", 200)))
        if name == "tool_view_image": return tool_view_image(args["path"])
        if name == "tool_view_pdf_page": return tool_view_pdf_page(args["path"], int(args["page"]))
        if name == "tool_web_search": return _text_block(web_search(args["query"], args.get("allowed_domains", [])))
        if name == "tool_web_fetch": return _text_block(web_fetch(args["url"]))
        return _text_block(f"Error: Unknown tool {name}")
    except Exception as e:
        return _text_block(f"Tool execution error: {e}")

def _truncate_text_blocks(blocks: list, limit: int = 10000) -> list:
    """Cap oversized TEXT results so one huge tool output can't blow up the
    conversation. Deliberately leaves IMAGE blocks untouched -- truncating
    base64 image data would corrupt the picture rather than shrink it."""
    out = []
    for b in blocks:
        if b.get("type") == "text" and len(b["text"]) > limit:
            out.append({"type": "text", "text": b["text"][:limit] + "... [TRUNCATED]"})
        else:
            out.append(b)
    return out

def run_generate(target_dir: Path = None, live_mode: bool = False, verbose: bool = False) -> int:
    """The whole of Stage 2, run start to finish for ONE chapter. This is
    the single function src/main.py calls to do "note generation" for one
    pipeline run. Returns an exit code (see EXIT_OK/EXIT_RATE_LIMITED/
    EXIT_FATAL in src/func_tools_and_utils.py) telling the caller what
    happened.

    Overall shape:
      1. Pick which chapter folder to work on (an explicit `target_dir`,
         or auto-pick the next ready one via select_target_chapter()).
      2. If not `live_mode`: stop here, having spent zero API tokens (see
         "MOCK MODE" below) -- useful for testing the pipeline's plumbing
         without any cost.
      3. If `live_mode`: run the AGENTIC LOOP described in this module's
         docstring at the top of the file -- repeatedly asking Claude what
         to do next and running whichever tools it requests -- until
         Claude signals it's done, the turn/attempt budget runs out, or an
         unrecoverable error occurs. Progress is saved to a JSON file after
         every turn, so an interrupted run can RESUME from where it left
         off next time instead of starting over from turn 0."""
    logger.info(f"=== Stage 2: Agentic Note Generator (live_mode={live_mode}) ===")

    target_root = config.DEFAULT_TARGET_ROOT
    if not target_dir:
        target_dir = select_target_chapter(target_root)

    if not target_dir:
        logger.info("No chapter folder ready for note generation. Nothing to do.")
        logger.info("----------------------------------------------------------------------")
        return EXIT_OK
    # Defensive: main.py always passes a Path, but coerce here too in case
    # some other caller (or a test) hands this a plain string -- cheap and
    # a no-op if it's already a Path.
    target_dir = Path(target_dir)

    if not target_dir.is_dir():
        # Only reachable via an explicit --target-dir: select_target_chapter()
        # above only ever returns folders that already exist. Without this
        # check, a typo'd --target-dir silently "succeeds" -- 0 transcripts
        # and 0 supporting files found is indistinguishable from a real
        # chapter that genuinely has no input yet (confirmed live: returns
        # EXIT_OK either way otherwise).
        logger.error(f"--target-dir path does not exist or is not a directory: {target_dir}")
        return EXIT_FATAL

    logger.info(f"Target chapter directory: {target_dir}")

    progress_dir = config.CHAPTER_PROGRESS_DIR
    progress_dir.mkdir(parents=True, exist_ok=True)
    progress_file = progress_dir / f"{target_dir.name}.json"

    transcripts_dir = target_dir / config.TRANSCRIPTS_DIR
    top_transcripts = [f for f in transcripts_dir.glob("*") if f.is_file() and not is_ignorable(f)] if transcripts_dir.exists() else []
    sup_dir = target_dir / config.SUP_DIR
    sup_files = [f for f in sup_dir.glob("*") if f.is_file() and not is_ignorable(f)] if sup_dir.exists() else []

    expected_docx = target_dir / f"{target_dir.name}.docx"

    logger.info("Input files identified for processing:")
    logger.info(f"  [Spine Transcripts] ({len(top_transcripts)} files):")
    for f in top_transcripts:
        logger.info(f"    - {f.name} ({f.stat().st_size} bytes)")
    logger.info(f"  [Supporting Materials] ({len(sup_files)} files):")
    for f in sup_files:
        logger.info(f"    - {f.name} ({f.stat().st_size} bytes)")
    logger.info(f"  [Target Output Document]: {expected_docx.name}")

    if not live_mode:
        # MOCK MODE: genuinely ZERO Anthropic API calls, not just a "small"
        # one. An earlier version of this branch still fired one tiny
        # metadata ping to the API here (to verify the SDK/key work) -- a
        # real, if small, token/request cost. That's no longer acceptable
        # for a mode whose whole purpose is "assembly may spend LLM tokens
        # tonight, but generation must spend exactly none" -- so this now
        # only checks that a client COULD be constructed (i.e. the key is
        # present and well-formed enough for the SDK to accept), without
        # ever actually talking to the network. `doctor` (main.py) and the
        # Stage 1 router calls are what actually exercise the real API key
        # end-to-end -- this mode intentionally does not duplicate that.
        logger.info("----------------------------------------------------------------------")
        logger.info("[MOCK MODE ACTIVE] Zero-token safety is ON for note GENERATION -- no generation-stage API calls are made (Stage 1 assembly/routing above may still have used the LLM).")
        if client:
            logger.info("Anthropic client initialized OK (key present); skipping any real API call by design.")
        else:
            logger.warning("No Anthropic API client initialized (key missing/invalid) -- '--live' generation would fail.")

        logger.info("[MOCK MODE]: Skipped full generation loop. 0 tokens consumed.")
        logger.info("Pass '--live' to enable full multi-turn .docx generation.")
        TokenTracker().log_summary(logger, "Stage 2 (Agentic Note Generator)")
        logger.info("Stage 2 Agentic Note Generator run complete cleanly.")
        logger.info("----------------------------------------------------------------------")
        return EXIT_OK

    # ==================== LIVE MODE ====================
    if not client:
        logger.error("Anthropic API key missing or client failed to initialize.")
        return EXIT_FATAL

    logger.info("Entering LIVE generation mode...")
    dev_mode = getattr(config, 'DEV_TOKEN_SAVER_MODE', False)
    if dev_mode:
        # Same cost-safe smoke-test toggle as src/agents/ (see
        # src/agents/__init__.py's DEV_TOKEN_SAVER_MODE section) -- cheap
        # model, dummy prompt, capped output, so this real multi-turn loop
        # (progress file, MAX_ATTEMPTS, MAX_TURNS, execute_tool) can be
        # exercised end-to-end for pennies instead of real generation-scale
        # cost. Tool calls still execute for real (unlike src/agents/tools.py,
        # which fakes most of its tools) -- AGENT_TOOLS here are generic
        # read/write/bash, not the skill's own heavy build scripts, so
        # there's nothing expensive to fake at that layer.
        logger.info("[LIVE MODE] DEV_TOKEN_SAVER_MODE active -- cheap model, dummy prompt, capped output.")
        generator_model = getattr(config, 'FIGURE_MODEL', 'claude-haiku-4-5')
        max_tokens = 50
        system_prompt = "You are a test agent in DEV_TOKEN_SAVER_MODE. Call at most one tool, then stop."
        default_messages = [{"role": "user", "content": "This is a cost-safe smoke test of the generation loop's wiring. Do nothing further."}]
    else:
        generator_model = config.GENERATOR_MODEL
        max_tokens = 8192
        system_prompt = f"You are an expert study-notes generator.\n\nSKILL DEFINITION:\n{load_skill_prompt()}"
        default_messages = [{"role": "user", "content": build_user_prompt(target_dir, top_transcripts, sup_files, expected_docx)}]

    # Bounded web-enrichment tools are offered only when opted in (off by
    # default). Behavior + domain/budget limits live in src/common/web_enrichment.py
    # and this loop; the model can't reach the web otherwise.
    active_tools = list(AGENT_TOOLS)
    if getattr(config, "ENABLE_WEB_ENRICHMENT", False):
        active_tools = active_tools + WEB_TOOLS

    # Deterministic run-log scoping for the retrospective. The skill's own
    # scripts -- run by the MODEL via tool_bash -- log to
    # $STUDY_NOTES_RUNDIR/run.jsonl (tools/_log.py; default '.study-notes'
    # relative to cwd). tool_bash inherits THIS process's env, so setting the
    # var here points every such log at this chapter's local scratch workspace
    # regardless of the cwd the model picks, letting capture_retro_findings()
    # read a log that belongs to just this chapter. sync_skill_package() puts a
    # copy of retro.py (and the rest of the skill) on disk for us to run it.
    # Skipped in dev mode: the dummy prompt never runs the skill's scripts.
    chapter_workspace = _chapter_workspace(target_dir)
    if not dev_mode:
        os.environ["STUDY_NOTES_RUNDIR"] = str(chapter_workspace / ".study-notes")
        try:
            sync_skill_package()
        except Exception as e:
            logger.warning(f"Could not pre-sync skill package for retro support: {e}")

    # `messages` is the running back-and-forth conversation with Claude:
    # our instructions, its replies, and every tool result, all in order.
    # Every new turn (below) sends the WHOLE conversation so far again --
    # Claude has no memory of its own between API calls, so this list IS
    # its only memory of what's happened.
    messages = default_messages
    start_turn = 0
    attempts = 0
    # Per-chapter web-enrichment budget/audit counters. Persisted in the
    # progress file so the budget survives a crash/rate-limit resume rather
    # than resetting to full each attempt.
    web_searches = 0
    web_fetches = 0
    web_sources = []
    # If a PREVIOUS run of this same chapter got interrupted partway
    # through (crash, rate limit, process killed), its conversation state
    # was saved to progress_file -- load it back so this run RESUMES from
    # that exact point instead of starting the chapter over from scratch.
    if progress_file.exists():
        try:
            state = json.loads(progress_file.read_text(encoding="utf-8"))
            messages = state.get("messages", default_messages)
            start_turn = state.get("turn", 0)
            attempts = state.get("attempts", 0)
            web_searches = state.get("web_searches", 0)
            web_fetches = state.get("web_fetches", 0)
            web_sources = state.get("web_sources", [])
            logger.info(f"Resuming from turn {start_turn} with {len(messages)} messages (attempt {attempts + 1}).")
        except Exception as e:
            logger.warning(f"Failed to load progress file ({e}); starting fresh.")

    attempts += 1
    if attempts > config.MAX_ATTEMPTS:
        logger.error(f"Max attempts ({config.MAX_ATTEMPTS}) reached for {target_dir.name}. Placing failure marker.")
        (target_dir / config.FAILMARK).write_text(
            f"Generation did not complete within {config.MAX_ATTEMPTS} attempts "
            f"(each attempt ended in a retryable API error). Delete this file to retry "
            f"from scratch.\n", encoding="utf-8")
        progress_file.unlink(missing_ok=True)
        return EXIT_FATAL

    def save_progress(turn: int):
        try:
            progress_file.write_text(
                json.dumps({"turn": turn, "messages": messages, "attempts": attempts,
                            "web_searches": web_searches, "web_fetches": web_fetches,
                            "web_sources": web_sources}),
                encoding="utf-8")
        except Exception as e:
            logger.warning(f"Could not save progress file: {e}")

    save_progress(start_turn)

    tracker = TokenTracker()
    try:
        # THE AGENTIC LOOP: one iteration = one "turn" = one round trip to
        # Claude. This keeps going, turn after turn, until Claude stops
        # requesting tools (meaning it believes the chapter is finished) or
        # the turn budget (config.MAX_TURNS) runs out.
        for turn in range(start_turn, config.MAX_TURNS):
            logger.info(f"Generation loop turn {turn + 1}/{config.MAX_TURNS}...")
            # Send the ENTIRE conversation so far, plus the tool menu
            # (AGENT_TOOLS), and get Claude's next reply back.
            resp = client.messages.create(
                model=generator_model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=messages,
                tools=active_tools,
            )
            tracker.record(generator_model, getattr(resp, "usage", None))

            text_blocks = [b.text for b in resp.content if b.type == "text"]
            if text_blocks:
                logger.info(f"Claude: {text_blocks[0][:200].strip()}...")

            # Claude's reply is a list of "blocks" -- some plain text
            # (its running commentary) and/or "tool_use" blocks (requests
            # to run a specific tool with specific arguments). Record the
            # WHOLE reply, as-is, into the conversation history so the next
            # turn has full context of what Claude just said/asked for.
            content_list = []
            for b in resp.content:
                if b.type == "text":
                    content_list.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    content_list.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            messages.append({"role": "assistant", "content": content_list})

            # No tool_use blocks in this reply == Claude's way of saying
            # "I'm done". Don't just take its word for it though: verify
            # the actual .docx file it was supposed to produce genuinely
            # exists on disk before declaring success.
            tool_calls = [b for b in resp.content if b.type == "tool_use"]
            if not tool_calls:
                logger.info("Claude finished generation without calling more tools.")
                if expected_docx.exists():
                    logger.info("Target docx confirmed. Placing success marker.")
                    # Best-effort post-run: web-sources manifest + retrospective
                    # capture/auto-fix. Success-gated (only a proven .docx gets
                    # here) and wrapped in its own try/except so a stray error in
                    # this bookkeeping can never turn a completed chapter into a
                    # failure. Mirrors the agents/subprocess gate. capture_retro_findings
                    # reads chapter_workspace/.study-notes/run.jsonl -- the same
                    # path STUDY_NOTES_RUNDIR scoped the skill scripts' logging to.
                    try:
                        if getattr(config, "ENABLE_WEB_ENRICHMENT", False):
                            write_web_sources_manifest(target_dir, web_sources)
                        if not dev_mode:
                            retro = capture_retro_findings(target_dir, chapter_workspace)
                            if retro["candidates"] and getattr(config, "ENABLE_AUTO_SKILL_IMPROVEMENT", False):
                                logger.warning(f"{len(retro['candidates'])} skill-improvement candidate(s) found; "
                                               f"attempting autonomous fix (regress.py-gated)...")
                                apply_retro_fixes(retro["candidates"], chapter_workspace)
                    except Exception as e:
                        logger.warning(f"Post-run retro/manifest step failed (chapter still OK): {e}")
                    (target_dir / config.MARKER).write_text("Done", encoding="utf-8")
                    progress_file.unlink(missing_ok=True)
                    return EXIT_OK
                else:
                    logger.warning("Agent stopped but docx missing. Placing failure marker.")
                    (target_dir / config.FAILMARK).write_text(
                        "Agent finished without calling any more tools, but the expected "
                        f".docx ({expected_docx.name}) was not found.\n", encoding="utf-8")
                    progress_file.unlink(missing_ok=True)
                    return EXIT_FATAL

            # Claude DID request one or more tools -- actually run each one
            # (execute_tool(), defined above) and package every result as a
            # "tool_result" block tagged with that tool call's own ID, so
            # Claude can tell which result answers which request. These
            # results become the next message sent back to Claude, closing
            # the loop for another turn.
            tool_results = []
            for t in tool_calls:
                logger.info(f"  Executing tool: {t.name}")
                # Enforce the per-chapter web budget in Python (mirrors the
                # agents loop) -- the model's own restraint is never trusted.
                # A cap hit returns an error tool_result instead of running,
                # so the model can keep working with its other tools.
                if t.name == "tool_web_search":
                    if web_searches >= config.MAX_WEB_SEARCHES_PER_CHAPTER:
                        tool_results.append({"type": "tool_result", "tool_use_id": t.id, "is_error": True,
                                             "content": _text_block("Error: web search budget for this chapter is exhausted; do not search again.")})
                        continue
                    web_searches += 1
                elif t.name == "tool_web_fetch":
                    if web_fetches >= config.MAX_WEB_SEARCHES_PER_CHAPTER:
                        tool_results.append({"type": "tool_result", "tool_use_id": t.id, "is_error": True,
                                             "content": _text_block("Error: web fetch budget for this chapter is exhausted; do not fetch again.")})
                        continue
                    web_fetches += 1
                blocks = _truncate_text_blocks(execute_tool(t.name, t.input))
                if t.name == "tool_web_fetch":
                    url = t.input.get("url")
                    if url and url not in web_sources:
                        web_sources.append(url)
                tool_results.append({"type": "tool_result", "tool_use_id": t.id, "content": blocks})
            messages.append({"role": "user", "content": tool_results})

            save_progress(turn + 1)

        logger.error(f"Max turns ({config.MAX_TURNS}) reached without finishing. Placing failure marker.")
        (target_dir / config.FAILMARK).write_text(
            f"Generation did not finish within {config.MAX_TURNS} turns in a single attempt "
            "-- this usually means the model got stuck in a loop rather than making "
            "progress. Delete this file to retry from scratch.\n", encoding="utf-8")
        progress_file.unlink(missing_ok=True)
        return EXIT_FATAL

    except Exception as e:
        # classify_api_error() (src/func_tools_and_utils.py) inspects what
        # went wrong and decides: is this a TEMPORARY problem worth
        # retrying later (e.g. the API is rate-limited or briefly
        # overloaded), or a PERMANENT one (e.g. a malformed request) that
        # retrying won't fix? A temporary error keeps the progress file
        # (so the next run resumes this same attempt); a permanent one
        # gives up on this chapter and records why.
        info = classify_api_error(e)
        if info["retry"]:
            write_retry_epoch(info["retry_epoch"])
            logger.warning(f"Pausing for retry ({info['reason']}). Progress kept for resume.")
            return EXIT_RATE_LIMITED
        logger.error(f"Fatal, non-retryable error during generation: {info['reason']}")
        (target_dir / config.FAILMARK).write_text(f"Fatal error: {info['reason']}\n", encoding="utf-8")
        progress_file.unlink(missing_ok=True)
        return EXIT_FATAL
    finally:
        tracker.log_summary(logger, "Stage 2 (Agentic Note Generator)")
        logger.info("----------------------------------------------------------------------")

# This block only runs if someone executes `python3 stage2_api.py` directly
# (e.g. for manual testing) -- in normal pipeline operation, src/main.py
# imports and calls run_generate() itself instead, this block never runs.
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 2 Agentic Study Notes Generator")
    parser.add_argument("--live", action="store_true", help="Run full live LLM generation instead of default mock mode")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()
    sys.exit(run_generate(live_mode=args.live, verbose=args.verbose))

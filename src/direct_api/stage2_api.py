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
    return f"""You are running fully unattended. Use the study-notes skill (given to you as
your system prompt) to generate ONE chapter's study notes.

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
    logger.info(f"=== Stage 2: Agentic Note Generator (live_mode={live_mode}) ===")

    target_root = config.DEFAULT_TARGET_ROOT
    if not target_dir:
        target_dir = select_target_chapter(target_root)

    if not target_dir:
        logger.info("No chapter folder ready for note generation. Nothing to do.")
        logger.info("----------------------------------------------------------------------")
        return EXIT_OK

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

    messages = default_messages
    start_turn = 0
    attempts = 0
    if progress_file.exists():
        try:
            state = json.loads(progress_file.read_text(encoding="utf-8"))
            messages = state.get("messages", default_messages)
            start_turn = state.get("turn", 0)
            attempts = state.get("attempts", 0)
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
                json.dumps({"turn": turn, "messages": messages, "attempts": attempts}),
                encoding="utf-8")
        except Exception as e:
            logger.warning(f"Could not save progress file: {e}")

    save_progress(start_turn)

    tracker = TokenTracker()
    try:
        for turn in range(start_turn, config.MAX_TURNS):
            logger.info(f"Generation loop turn {turn + 1}/{config.MAX_TURNS}...")
            resp = client.messages.create(
                model=generator_model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=messages,
                tools=AGENT_TOOLS,
            )
            tracker.record(generator_model, getattr(resp, "usage", None))

            text_blocks = [b.text for b in resp.content if b.type == "text"]
            if text_blocks:
                logger.info(f"Claude: {text_blocks[0][:200].strip()}...")

            content_list = []
            for b in resp.content:
                if b.type == "text":
                    content_list.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    content_list.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            messages.append({"role": "assistant", "content": content_list})

            tool_calls = [b for b in resp.content if b.type == "tool_use"]
            if not tool_calls:
                logger.info("Claude finished generation without calling more tools.")
                if expected_docx.exists():
                    logger.info("Target docx confirmed. Placing success marker.")
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

            tool_results = []
            for t in tool_calls:
                logger.info(f"  Executing tool: {t.name}")
                blocks = _truncate_text_blocks(execute_tool(t.name, t.input))
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 2 Agentic Study Notes Generator")
    parser.add_argument("--live", action="store_true", help="Run full live LLM generation instead of default mock mode")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()
    sys.exit(run_generate(live_mode=args.live, verbose=args.verbose))

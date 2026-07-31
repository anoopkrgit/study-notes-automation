"""
stage2.py -- Stage 2 (note generation) via the `claude` CLI subprocess,
billed against a Claude subscription instead of the metered Anthropic API
key (see src/claude_cli_subprocess/__init__.py for how this compares to
src/direct_api/ and src/agents/).

WHY THIS MODULE IS SHAPED DIFFERENTLY FROM direct_api.func_generate_notes
---------------------------------------------------------------------------
The legacy SDK implementation has to paste every transcript/supporting
file's actual TEXT CONTENT into the conversation, because a raw Anthropic
API call has no filesystem access of its own. The `claude` CLI is
different: given `--add-dir <chapter_folder>`, Claude Code can read and
write real files in that folder directly. So this module's prompt (see
build_cli_prompt()) is short -- it names FOLDER PATHS, not file contents --
and delegates the actual multi-step pipeline (ingest -> author JSON ->
validate -> figbuild -> verify -> build -> qa) to the study-notes skill
package itself (templates/study-notes.skill), synced onto local disk by
sync_skill_package() before the CLI call.

ONE call per attempt, not a hand-rolled multi-turn loop: unlike
direct_api.func_generate_notes's turn-by-turn tool loop, this module makes
a SINGLE `claude -p ...` subprocess call per attempt. Claude Code's own
session persistence (`-r/--resume <session_id>`) is what allows a later
attempt to continue an interrupted run, instead of us replaying full
message history the way the legacy progress file does.

TRUTH-CHECK, NOT SELF-REPORTED SUCCESS: exactly like the other two
implementations, whether the CLI's own JSON envelope claims success is
NEVER trusted on its own -- expected_docx.exists() is the only thing that
gates writing config.MARKER. A model can say "done" and be wrong.
"""

import json
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import settings as config
from src.func_tools_and_utils import (
    logger, write_retry_epoch, EXIT_OK, EXIT_RATE_LIMITED, EXIT_FATAL
)
from .common import build_claude_env, is_usage_limit


def sync_skill_package() -> Path:
    """Ensure the current templates/*.skill package is unzipped and
    up to date at config.CLAUDE_SKILL_INSTALL_DIR, re-extracting only when
    the source .skill file has changed (compared by mtime -- a single
    curated file a human drops in rarely, not something worth hashing on
    every chapter run). Returns the extracted directory path, for LOGGING
    ONLY -- never hardcode this path into the CLI prompt itself; the
    skill's own SKILL.md text says paths must never be hardcoded, Claude
    Code resolves its own skill directory at runtime.
    """
    install_dir = config.CLAUDE_SKILL_INSTALL_DIR
    candidates = sorted(
        config.LOCAL_RUNTIME_ROOT.glob(config.CLAUDE_SKILL_SOURCE_GLOB),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        logger.warning(f"No skill package found matching {config.CLAUDE_SKILL_SOURCE_GLOB}; "
                        f"leaving {install_dir} as-is.")
        return install_dir

    source = candidates[0]
    marker_file = install_dir / ".synced_from"
    stamp = f"{source}|{source.stat().st_mtime}"
    if install_dir.exists() and marker_file.exists():
        try:
            if marker_file.read_text(encoding="utf-8").strip() == stamp:
                logger.info(f"Skill package unchanged ({source.name}); skipping re-extract.")
                return install_dir
        except Exception:
            pass  # fall through and re-extract if the stamp is unreadable

    logger.info(f"Syncing skill package {source} -> {install_dir} ...")
    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source, "r") as zf:
        zf.extractall(install_dir)
    marker_file.write_text(stamp, encoding="utf-8")
    logger.info(f"Skill package synced ({source.stat().st_size} bytes from {source.name}).")
    return install_dir


def _chapter_workspace(target_dir: Path) -> Path:
    """The LOCAL scratch cwd for the `claude` CLI call -- deliberately NOT
    target_dir itself (which lives on synced Google Drive storage).
    npm install/node_modules/build artifacts belong on local disk, not
    Drive-synced storage. One persistent subfolder per chapter (named after
    target_dir.name) so a resumed attempt's node_modules survives instead
    of being rebuilt from scratch on every retry -- that matters for both
    cost (npm install time) and speed."""
    ws = config.CLAUDE_WORKSPACE_ROOT / target_dir.name
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def build_cli_prompt(target_dir: Path, expected_docx: Path, resume: bool = False) -> str:
    """The per-run instructions given to `claude -p`. Deliberately SHORT
    and points at FOLDER PATHS, not file contents -- unlike
    direct_api.func_generate_notes's build_user_prompt(), which has to
    paste file content into the message because a raw API call has no
    filesystem access. This CLI call has real filesystem access via
    --add-dir, so it doesn't need that.

    resume=True builds a SHORT continuation prompt instead of the full
    invocation prompt -- Claude Code's own session transcript (resumed via
    -r/--resume) already remembers everything from the first prompt;
    re-sending the full prompt into an already-primed session would waste
    tokens/turns re-explaining what it already knows.
    """
    if resume:
        return (
            "Continue the study-notes generation you were working on in this "
            "session. Pick up wherever you left off and finish producing the "
            f"final .docx at exactly this path: {expected_docx}\n"
            "Do not start over from scratch if partial progress already exists."
        )

    transcripts_dir = target_dir / config.TRANSCRIPTS_DIR
    sup_dir = target_dir / config.SUP_DIR
    return f"""/study-notes

Generate ONE chapter's print-ready study-notes .docx, fully unattended -- do not pause for
confirmation and do not ask any questions.

Chapter folder: {target_dir}
Class lecture transcripts (the SPINE -- define scope, completeness against these is
non-negotiable): {transcripts_dir}
Supporting/reference material (enrichment only -- must NOT expand scope beyond the
transcripts): {sup_dir}

Ignore any "{config.SOURCES}" manifest file, "{config.HOLD}", "{config.NEWMAT}", "_prev/"
(old versions), and OS/cloud-sync metadata files (desktop.ini, Thumbs.db, .DS_Store).

Infer the subject (Physics / Chemistry / Maths) and chapter name from the contents.

Save the final .docx to exactly this path: {expected_docx}

Make reasonable assumptions where inputs are ambiguous and proceed to completion."""


def run_claude_cli(target_dir: Path, prompt: str, resume_session_id: str = None) -> dict:
    """Make ONE `claude -p ...` subprocess call for this chapter and return
    a plain result dict: {"ok", "result", "session_id", "total_cost_usd",
    "error", "returncode", "raw_stdout"}.

    cwd is the LOCAL scratch workspace (see _chapter_workspace), NOT
    target_dir -- npm install/build artifacts don't belong on synced Drive
    storage. --add-dir extends the CLI's filesystem access to the real
    chapter folder so it can read transcripts and write the final .docx
    there. Never passes --dangerously-skip-permissions (unlike Stage 1's
    read-only router) -- this call WRITES files, so it needs real
    permission handling (--permission-mode acceptEdits).

    Output-format parsing is DEFENSIVE by design: the exact JSON envelope
    shape of `claude -p ... --output-format json` has not been empirically
    confirmed yet (see this module's pilot-verification notes in the
    implementation plan) -- every field is read with .get(...), never a
    hard KeyError, and stdout parsing falls back gracefully if the
    expected envelope isn't found.
    """
    dev_mode = getattr(config, 'DEV_TOKEN_SAVER_MODE', False)
    workspace = _chapter_workspace(target_dir)
    # DEV_TOKEN_SAVER_MODE gets NO tool access at all (empty --allowedTools),
    # not just a cheap prompt -- --max-budget-usd only stops the run AFTER
    # it's exceeded, not before, and a full agentic session with the real
    # production tool list (Bash python3/node/npm/pdftotext/soffice, Write,
    # Edit -- the whole skill's toolbox) can spend real money exploring
    # before the budget check catches up, even against a "just acknowledge
    # and stop" prompt the model doesn't perfectly follow. Confirmed live:
    # an earlier version of this function that always granted
    # config.CLAUDE_ALLOWED_TOOLS spent $0.145 (with reported
    # input/output_tokens both 0 -- the cost came from tool/subagent use
    # the top-level usage object doesn't itemize) on a single dev-mode call
    # before hitting error_max_budget_usd. With no tools granted, the model
    # can only reply in text -- nothing to spend money doing.
    allowed_tools = "" if dev_mode else config.CLAUDE_ALLOWED_TOOLS
    cmd = [config.CLAUDE_BIN, "-p", prompt,
           "--output-format", "json",
           "--permission-mode", config.CLAUDE_PERMISSION_MODE,
           "--allowedTools", allowed_tools,
           "--add-dir", str(target_dir)]
    if getattr(config, 'CLAUDE_MODEL', ''):
        cmd += ["--model", config.CLAUDE_MODEL]
    if resume_session_id:
        # Exact flag spelling unverified against `claude -p --help` at
        # implementation time -- pilot-verification item, see plan step 1.
        cmd += ["-r", resume_session_id]
    if dev_mode:
        # Same cost-safe smoke-test toggle Stage 1 uses (see stage1.py's
        # run_router()) -- a hard per-call dollar ceiling and a lower
        # reasoning-effort setting, since the CLI has no max_tokens flag.
        # Belt-and-suspenders alongside the empty --allowedTools above.
        cmd += ["--max-budget-usd", str(config.DEV_TOKEN_SAVER_MAX_BUDGET_USD),
                "--effort", config.DEV_TOKEN_SAVER_EFFORT]

    logger.info(f"Invoking claude CLI for chapter '{target_dir.name}' "
                f"(cwd={workspace}, resume={'yes' if resume_session_id else 'no'}) ...")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(workspace),
                            env=build_claude_env(),
                            timeout=config.CLAUDE_CLI_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.error(f"claude CLI call timed out after {config.CLAUDE_CLI_TIMEOUT_SECONDS}s.")
        return {"ok": False, "result": None, "session_id": None, "total_cost_usd": None,
                "error": "timeout", "returncode": None, "raw_stdout": ""}
    except Exception as e:
        logger.error(f"claude CLI call failed to launch: {e}")
        return {"ok": False, "result": None, "session_id": None, "total_cost_usd": None,
                "error": f"launch failure: {e}", "returncode": None, "raw_stdout": ""}

    combined = (p.stdout or "") + "\n" + (p.stderr or "")

    envelope = None
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                candidate = json.loads(line)
                if isinstance(candidate, dict):
                    envelope = candidate
                    break
            except Exception:
                continue
    if envelope is None:
        # Fall back: maybe the whole stdout is one JSON document, not
        # line-delimited (unconfirmed which shape --output-format json
        # actually produces for `claude -p` -- see plan's pilot-verification notes).
        try:
            envelope = json.loads(p.stdout or "")
        except Exception:
            m = re.search(r"\{.*\}", p.stdout or "", re.S)
            if m:
                try:
                    envelope = json.loads(m.group(0))
                except Exception:
                    envelope = None

    envelope = envelope or {}
    session_id = envelope.get("session_id")
    total_cost_usd = envelope.get("total_cost_usd")
    result_text = envelope.get("result")
    is_error = bool(envelope.get("is_error"))

    if p.returncode != 0 or is_error:
        error_text = result_text or (p.stderr or "").strip() or combined.strip()
        logger.warning(f"claude CLI call did not succeed cleanly "
                        f"(returncode={p.returncode}, is_error={is_error}): {error_text[:500]}")
        return {"ok": False, "result": result_text, "session_id": session_id,
                "total_cost_usd": total_cost_usd, "error": error_text,
                "returncode": p.returncode, "raw_stdout": p.stdout or ""}

    logger.info(f"claude CLI call finished (session_id={session_id}, "
                f"total_cost_usd={total_cost_usd}).")
    return {"ok": True, "result": result_text, "session_id": session_id,
            "total_cost_usd": total_cost_usd, "error": None,
            "returncode": p.returncode, "raw_stdout": p.stdout or ""}


def classify_cli_result(result: dict) -> dict:
    """Decide what to do after a claude CLI call did NOT succeed (only
    called when result["ok"] is False). Same {retry, retry_epoch, reason}
    shape as func_tools_and_utils.classify_api_error(), so it plugs into
    the same write_retry_epoch() machinery.

    There is no typed exception to catch here -- only a plain error-text
    string (result["error"]) plus a returncode -- so this is pattern
    matching, not isinstance() dispatch.

    JUDGMENT CALL: an error that matches none of the known patterns is
    classified as NON-retryable (FATAL) by default, not transient -- see
    the implementation plan's "Judgment calls" section for the full
    reasoning (Stage 2 failures are higher-stakes than Stage 1 routing
    failures, so unknown-shaped errors default to surfacing immediately
    rather than being assumed transient).
    """
    now = int(time.time())
    error_text = result.get("error") or ""

    if error_text == "timeout":
        return {"retry": True, "retry_epoch": now + config.CLAUDE_RETRY_EPOCH_DEFAULT_SECONDS,
                "reason": f"claude CLI call timed out after {config.CLAUDE_CLI_TIMEOUT_SECONDS}s"}

    if error_text.startswith("launch failure:"):
        # `claude` binary missing/misconfigured, permission error launching
        # the process, etc. Retryable -- could be transient (PATH not yet
        # set up on a cold-started scheduled task) rather than a true bug.
        return {"retry": True, "retry_epoch": now + config.CLAUDE_RETRY_EPOCH_DEFAULT_SECONDS,
                "reason": f"failed to launch claude CLI: {error_text}"}

    if is_usage_limit(error_text):
        return {"retry": True, "retry_epoch": now + config.CLAUDE_RETRY_EPOCH_DEFAULT_SECONDS,
                "reason": f"claude CLI usage limit detected: {error_text[:300]}"}

    # Unrecognized error shape: default to FATAL (see docstring's judgment
    # call above). Logged in full so classify_cli_result()'s patterns can
    # be tuned once real error text is observed in the pilot (verification
    # step 5).
    return {"retry": False, "retry_epoch": None,
            "reason": f"unrecognized/non-retryable claude CLI error: {error_text[:500]}"}


def run_stage2_chapter(target_dir: Path = None, live_mode: bool = False) -> int:
    """Stage 2's subprocess entrypoint -- same contract shape as
    direct_api.func_generate_notes.run_generate() and
    agents.stage2_graph.run_stage2_chapter(): returns EXIT_OK,
    EXIT_RATE_LIMITED, or EXIT_FATAL.
    """
    logger.info(f"=== Stage 2: claude CLI Subprocess Generator (live_mode={live_mode}) ===")

    if not target_dir:
        from src.direct_api.func_generate_notes import select_target_chapter
        target_dir = select_target_chapter(config.DEFAULT_TARGET_ROOT)
        if not target_dir:
            logger.info("No chapter folder ready for note generation. Nothing to do.")
            return EXIT_OK

    logger.info(f"Target chapter directory: {target_dir}")
    expected_docx = target_dir / f"{target_dir.name}.docx"

    if not live_mode:
        logger.info("[MOCK MODE ACTIVE] Zero-token safety is ON for note GENERATION -- "
                     "no claude CLI subprocess call is made.")
        logger.info("[MOCK MODE]: Skipped full generation loop. 0 tokens consumed.")
        return EXIT_OK

    # ==================== LIVE MODE ====================
    # Wrapped in a broad try/except, matching the same top-level resilience
    # pattern direct_api.func_generate_notes.run_generate() and
    # agents.stage2_graph.run_stage2_chapter() both use: ANY unexpected
    # failure here (a corrupted skill zip, a permissions error, a bug in
    # prompt-building -- anything outside run_claude_cli()'s own
    # try/except around the subprocess call itself) must degrade to a
    # clean FAILMARK + EXIT_FATAL, never an uncaught traceback that kills
    # the whole nightly process.
    progress_file = config.CHAPTER_PROGRESS_DIR / f"{target_dir.name}.json"
    try:
        sync_skill_package()

        config.CHAPTER_PROGRESS_DIR.mkdir(parents=True, exist_ok=True)

        session_id = None
        attempts = 0
        if progress_file.exists():
            try:
                state = json.loads(progress_file.read_text(encoding="utf-8"))
                session_id = state.get("session_id")
                attempts = state.get("attempts", 0)
                logger.info(f"Resuming session {session_id} (attempt {attempts + 1}).")
            except Exception as e:
                logger.warning(f"Failed to load progress file ({e}); starting fresh.")

        attempts += 1
        if attempts > config.MAX_ATTEMPTS:
            logger.error(f"Max attempts ({config.MAX_ATTEMPTS}) reached for {target_dir.name}. "
                          "Placing failure marker.")
            (target_dir / config.FAILMARK).write_text(
                f"Generation did not complete within {config.MAX_ATTEMPTS} attempts "
                f"(each attempt ended in a retryable claude CLI error). Delete this file to "
                f"retry from scratch.\n", encoding="utf-8")
            progress_file.unlink(missing_ok=True)
            return EXIT_FATAL

        if getattr(config, 'DEV_TOKEN_SAVER_MODE', False):
            # Dummy prompt that does NOT point at real transcripts and,
            # critically, never writes the literal string "/study-notes"
            # anywhere in it -- confirmed live that including that
            # substring risks the CLI parsing it as a real skill
            # invocation even mid-sentence, defeating the whole point of
            # this cheap smoke-test path. run_claude_cli() also grants
            # ZERO tools in dev mode (see its own comment), so this prompt
            # doesn't need to additionally instruct "don't use tools" --
            # there's nothing to invoke either way.
            prompt = ("You are a test agent exercising cost-safe smoke-test wiring. "
                       "Reply with a short acknowledgement only, then stop immediately.")
        else:
            prompt = build_cli_prompt(target_dir, expected_docx, resume=bool(session_id))

        result = run_claude_cli(target_dir, prompt, resume_session_id=session_id)

        def save_progress():
            try:
                progress_file.write_text(
                    json.dumps({"session_id": result.get("session_id") or session_id,
                                "attempts": attempts}),
                    encoding="utf-8")
            except Exception as e:
                logger.warning(f"Could not save progress file: {e}")

        if result["ok"] and expected_docx.exists():
            logger.info("Target docx confirmed. Placing success marker.")
            (target_dir / config.MARKER).write_text("Done", encoding="utf-8")
            progress_file.unlink(missing_ok=True)
            return EXIT_OK

        if result["ok"] and not expected_docx.exists():
            # The CLI reported success but the file genuinely isn't there --
            # NEVER trust self-reported success. Treat as fatal (not
            # retryable): a "successful" call that produced no output is a
            # logic bug, not a transient condition retrying would fix.
            logger.warning(f"claude CLI reported success but {expected_docx.name} was not found. "
                            "Placing failure marker.")
            (target_dir / config.FAILMARK).write_text(
                "claude CLI call completed without error, but the expected .docx "
                f"({expected_docx.name}) was not found afterward.\n", encoding="utf-8")
            progress_file.unlink(missing_ok=True)
            return EXIT_FATAL

        # result["ok"] is False from here on.
        info = classify_cli_result(result)
        if info["retry"]:
            write_retry_epoch(info["retry_epoch"])
            save_progress()
            logger.warning(f"Pausing for retry ({info['reason']}). Progress kept for resume.")
            return EXIT_RATE_LIMITED

        logger.error(f"Fatal, non-retryable error during generation: {info['reason']}")
        (target_dir / config.FAILMARK).write_text(f"Fatal error: {info['reason']}\n", encoding="utf-8")
        progress_file.unlink(missing_ok=True)
        return EXIT_FATAL
    except Exception as e:
        # Something broke outside run_claude_cli()'s own error handling
        # (e.g. sync_skill_package() hit a corrupted zip, a permissions
        # error creating the workspace/progress dirs) -- treat the whole
        # attempt as failed for this run rather than crashing the process.
        logger.exception(f"Unexpected error during Stage 2 subprocess generation: {e}")
        (target_dir / config.FAILMARK).write_text(f"Unexpected fatal error: {e}\n", encoding="utf-8")
        progress_file.unlink(missing_ok=True)
        return EXIT_FATAL

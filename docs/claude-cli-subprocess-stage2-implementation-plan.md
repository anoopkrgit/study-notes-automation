# Implementation plan: `src/claude_cli_subprocess/stage2.py`

## Context

The pipeline has three parallel, co-equal implementations of Stage 2 (note generation),
selected via `--stage2-impl {legacy,graph,subprocess}` and dispatched through
`src/agents/dispatch.py::generate_notes()`:

1. **legacy** (`src/direct_api/func_generate_notes.py::run_generate()`) — direct Anthropic SDK
   calls, fully built.
2. **graph** (`src/agents/stage2_graph.py::run_stage2_chapter()`) — LangGraph multi-agent
   flowchart, fully built.
3. **subprocess** (`src/claude_cli_subprocess/stage2.py::run_stage2_chapter()`) — invokes the
   `claude` CLI as a subprocess, billed via Claude subscription instead of the metered API key.
   **Currently a 6-line stub that raises `NotImplementedError`.** This is what this plan builds.

`src/agents/dispatch.py` already has the branch wired (`elif impl == 'subprocess': ... return
cli_run_stage2_chapter(target_dir, live_mode)`) — verified, needs zero changes. `src/main.py`
currently fails fast on `--stage2-impl subprocess` via `parser.error(...)`; once this module is
real, that fail-fast needs removing so the flag actually works end-to-end.

Design work for this already exists in `docs/cli-subprocess-plan.md`'s "Stage 2: full design"
section (environment facts, architecture, function list, config settings) — this plan turns
that into exact, verified, execution-ready code. All facts below were independently
re-verified against the actual current repo state (not assumed from the older doc), including
`claude --version` (2.1.197, confirmed installed), `~/.claude/.credentials.json` (present),
`templates/study-notes.skill` (79,219 bytes, confirmed present), and the exact current line
content of `config/settings.py`, `src/main.py`, `src/agents/dispatch.py`, and the sibling
`src/claude_cli_subprocess/stage1.py`/`common.py`.

## Interface contract this module must honor

`run_stage2_chapter(target_dir=None, live_mode=False) -> int`, matching `direct_api`'s and
`agents`'s exact same signature shape and exit-code contract (`EXIT_OK`=0, `EXIT_RATE_LIMITED`=42,
`EXIT_FATAL`=1, from `src/func_tools_and_utils.py`):

- **Chapter selection** (when `target_dir` is `None`): call `select_target_chapter(config.DEFAULT_TARGET_ROOT)`
  from `src.direct_api.func_generate_notes` — a shared utility, not reimplemented. Return
  `EXIT_OK` immediately if nothing is ready.
- **Mock mode** (`live_mode=False`): zero subprocess calls, log clearly, return `EXIT_OK`
  immediately.
- **`expected_docx = target_dir / f"{target_dir.name}.docx"`** — verified exact naming
  convention from `direct_api/func_generate_notes.py:288`.
- **Truth-check is mandatory**: `expected_docx.exists()` gates the success marker — a
  self-reported "success" from the CLI is never trusted on its own, including when the CLI's
  own JSON envelope claims success.
- **Success**: `(target_dir / config.MARKER).write_text("Done", ...)`, delete the progress
  file, return `EXIT_OK`.
- **Retryable failure** (rate limit, launch failure, timeout): `write_retry_epoch(retry_epoch)`
  (from `src.func_tools_and_utils`), keep the progress file, return `EXIT_RATE_LIMITED`.
- **Non-retryable failure / `config.MAX_ATTEMPTS` exceeded**: write `config.FAILMARK`, delete
  the progress file, return `EXIT_FATAL`.
- **`DEV_TOKEN_SAVER_MODE`**: set independently per stage via `--stage2-mode llm-token-saver`
  (already wired in `main.py`). Must use a dummy prompt that never points at real transcripts,
  plus the same two CLI flags Stage 1 already uses (`--max-budget-usd`, `--effort low`) — no
  SDK `max_tokens` to cap, these are the CLI-native cost-safety equivalent.
- **Environment scrubbing**: every `subprocess.run()` call to `claude` MUST pass
  `env=build_claude_env()` (already exists in `.common`, reused not redefined) — strips
  `ANTHROPIC_API_KEY` so the CLI bills the subscription, not the metered key.

## Judgment calls (flagged explicitly)

1. **Skill-changed detection: mtime, not content hash.** `templates/*.skill` is a single
   curated file a human drops in rarely; hashing a 79KB zip on every chapter run is needless
   I/O. A content hash would be strictly more correct against clock skew, but mtime is simpler
   and matches the original design doc's own "mtime or content hash — pick one" framing.
2. **Per-chapter local scratch workspace persists across attempts** — `config.CLAUDE_WORKSPACE_ROOT/<target_dir.name>/`,
   never deleted by this module, so `npm install`'s `node_modules` survives a resumed retry
   instead of being rebuilt from scratch (cost and speed). Workspace cleanup for abandoned
   chapters is out of scope for this plan (disk-space concern only, not correctness).
3. **`classify_cli_result()`: unrecognized errors default to FATAL, not retryable.** There's no
   typed exception here, only a plain error-text string, so this is pattern matching, not
   `isinstance()` dispatch. A real bug (bad flag, broken skill package) retried forever every
   night is worse than surfacing immediately via `FAILMARK` where a human sees it. This is
   deliberately different from Stage 1's router, where an unclear failure just means "fall back
   to filename routing" — low stakes. Stage 2 failures are a whole chapter's generation attempt
   — higher stakes, so default to FATAL.
4. **Progress file shape is `{"session_id": str|None, "attempts": int}`** — deliberately much
   smaller than legacy's `{"turn", "messages", "attempts"}`, since Claude Code's own
   `-r/--resume <session_id>` resumes the actual conversation via its own transcript
   persistence; no need to replay full message history ourselves.
5. **Two mechanics are explicitly unverified, flagged as pilot-verification items, not assumed**:
   - The exact `-r`/`--resume` flag spelling (confirm against `claude -p --help` before trusting it).
   - The exact JSON envelope shape of `claude -p ... --output-format json`'s stdout (field
     names for `session_id`, cost, success signal). `run_claude_cli()` below parses
     defensively (every field via `.get(...)`, never a hard `KeyError`) and reuses Stage 1's
     proven line-scan-then-whole-stdout-fallback parsing strategy from `stage1.py::run_router()`.
   Both get resolved in Verification step 1, *before* trusting the wrapper code.

## `config/settings.py` — insert after the existing Stage 1 "claude CLI settings" block (verified: currently ends right after `DEV_TOKEN_SAVER_EFFORT = ...`)

```python

# ---------------------------------------------------------------------------
# claude CLI settings for Stage 2 (src/claude_cli_subprocess/stage2.py).
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
```

No naming collisions — verified against the full current file.

## `src/main.py` — two diffs, both verified against exact current lines

**Diff A** (line 153-156, help text — remove "NOT YET IMPLEMENTED"):
```python
    parser.add_argument("--stage2-impl", choices=["legacy", "graph", "subprocess"], default="legacy",
                         help="Stage 2 implementation: 'legacy' monolithic loop (default), 'graph' "
                              "multi-agent flowchart (src/agents/stage2_graph.py), or 'subprocess' -- "
                              "the `claude` CLI billed via Claude subscription "
                              "(src/claude_cli_subprocess/stage2.py)")
```

**Diff B** (line 184-189 — delete the whole fail-fast block, including its trailing blank line,
so `main()` goes straight from `args = parser.parse_args()` to `if args.quiet:`):
```python
    if args.stage2_impl == "subprocess":
        parser.error(
            "--stage2-impl subprocess is not yet implemented -- see the 'Stage 2: full "
            "design' section of docs/cli-subprocess-plan.md. Use --stage2-impl legacy or "
            "--stage2-impl graph."
        )
```

## `src/agents/dispatch.py` — no change needed

Verified lines 66-69 already implement the subprocess branch as a lazy import; works
immediately once `stage2.py` is real.

## Documentation/comment updates needed

Four places across the repo currently say Stage 2 subprocess isn't built — all go stale/wrong
once it is. Found via `grep -rln "NOT YET IMPLEMENTED\|not yet built\|not yet implemented"`
across `*.py`/`*.sh`/`*.ps1`/`*.md` (excluding `web-enrichment-plan.md`, which uses the same
phrase for an unrelated, separate feature).

**1. `scripts/wsl-study-notes-processor.sh`'s `-h`/`--help` text** (verified exact current line 66):
```
          subprocess   src/claude_cli_subprocess/ headless `claude` CLI,
                         billed via Claude subscription, not the metered API key
                         Stage 1: available.
                         Stage 2: NOT YET IMPLEMENTED -- fails fast with a
                         clear error if requested; see the "Stage 2: full
                         design" section of docs/cli-subprocess-plan.md.
```
Change to match how `legacy`/`graph` are already worded (`Stage 1: available.  Stage 2: available.`):
```
          subprocess   src/claude_cli_subprocess/ headless `claude` CLI,
                         billed via Claude subscription, not the metered API key
                         Stage 1: available.  Stage 2: available.
```

**2. `docs/architecture.md`'s call graph** (verified exact current lines 51-52):
```
       └─ --stage2-impl subprocess ───────► run_stage2_chapter() [src/claude_cli_subprocess/stage2.py]
                                              └─► NotImplementedError  (not yet built -- see docs/cli-subprocess-plan.md)
```
Change to mirror the `legacy`/`graph` leaves already in that same diagram:
```
       └─ --stage2-impl subprocess ───────► run_stage2_chapter() [src/claude_cli_subprocess/stage2.py]
                                              └─► run_claude_cli() ─► subprocess.run(["claude","-p",...])   ⟵ claude CLI
```

**3. `config/settings.py` line 93** — this exact comment is in the REAL config file, not just
quoted in a doc:
```python
# both Stage 1 (built now) and Stage 2 (scaffolded; see cli-subprocess-plan.md).
```
Change to:
```python
# both Stage 1 and Stage 2 (see cli-subprocess-plan.md).
```

**4. `docs/cli-subprocess-plan.md` itself** — the master design doc this implementation plan
was built from — has three stale "scaffolded/build later" framings that become wrong once
Stage 2 is real:
- Line 46-49 (Decisions section): `**Stage 2 subprocess: scaffolded now** (clear NotImplementedError), full build deferred — no reference code exists for it, only the detailed design in this doc` — update to reflect it's now built, pointing at this implementation plan doc as what it was built from.
- Line 164: section header `## Stage 2: full design (build later — this is the ready-to-implement spec)` — update to drop "(build later...)", e.g. `## Stage 2: design (built — see docs/claude-cli-subprocess-stage2-implementation-plan.md for the executed implementation)`.
- The Stage 2 code-snippet quotes at lines 156/392 (the `NotImplementedError`/`parser.error` message text) are historical quotes of code that existed at the time of writing — leave as-is, they're accurately describing what the stub used to say, not a live claim.

## `src/claude_cli_subprocess/stage2.py` — full implementation

```python
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
    workspace = _chapter_workspace(target_dir)
    cmd = [config.CLAUDE_BIN, "-p", prompt,
           "--output-format", "json",
           "--permission-mode", config.CLAUDE_PERMISSION_MODE,
           "--allowedTools", config.CLAUDE_ALLOWED_TOOLS,
           "--add-dir", str(target_dir)]
    if getattr(config, 'CLAUDE_MODEL', ''):
        cmd += ["--model", config.CLAUDE_MODEL]
    if resume_session_id:
        # Exact flag spelling unverified against `claude -p --help` at
        # implementation time -- pilot-verification item, see plan step 1.
        cmd += ["-r", resume_session_id]
    if getattr(config, 'DEV_TOKEN_SAVER_MODE', False):
        # Same cost-safe smoke-test toggle Stage 1 uses (see stage1.py's
        # run_router()) -- a hard per-call dollar ceiling and a lower
        # reasoning-effort setting, since the CLI has no max_tokens flag.
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
    sync_skill_package()

    config.CHAPTER_PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    progress_file = config.CHAPTER_PROGRESS_DIR / f"{target_dir.name}.json"

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
        # Dummy prompt that does NOT point at real transcripts -- mirrors
        # stage1.py's route_file() dev-mode pattern. --max-budget-usd/
        # --effort are added inside run_claude_cli() itself.
        prompt = ("You are a test agent in DEV_TOKEN_SAVER_MODE for the /study-notes skill. "
                   "Do NOT read any real transcript or supporting file. Reply with a short "
                   "acknowledgement and stop immediately.")
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
```

Implementation notes on the code above:
- `expected_docx.exists()` gates success **even when `result["ok"]` is True** — its own branch,
  fatal not retryable, reasoning inline.
- `MAX_ATTEMPTS` check happens **before** the CLI call, mirroring legacy's exact placement —
  consistent ordering across all three implementations.
- `save_progress()` is only called on the retryable path — on fatal or success the progress
  file is deleted, matching legacy exactly.
- `sync_skill_package()` runs unconditionally in live mode (not gated by dev-token-saver) since
  it's a local file-copy with no API/CLI cost.

## Tests

### New file `tests/test_claude_cli_subprocess_stage2.py`

Follow `tests/test_claude_cli_subprocess_stage1.py`'s exact conventions (`sys.path` bootstrap,
`monkeypatch.setattr(config, ...)`, `patch('subprocess.run')`, asserting `env=build_claude_env()`
excludes `ANTHROPIC_API_KEY`). Cover:

1. `test_run_stage2_chapter_mock_mode_makes_zero_subprocess_calls` — `live_mode=False` never
   calls `subprocess.run`.
2. `test_run_stage2_chapter_dev_token_saver_uses_dummy_prompt_and_flags` — dummy prompt doesn't
   contain the real `target_dir` path; `--max-budget-usd`/`--effort` present; env scrubbed.
3. `test_success_marker_requires_docx_to_actually_exist` — CLI claims success but no file on
   disk → `EXIT_FATAL`, `FAILMARK` written, `MARKER` NOT written.
4. `test_success_marker_written_when_docx_exists` — real file created during the mocked call →
   `EXIT_OK`, `MARKER` written.
5. `test_nonzero_unparseable_returncode_is_retryable` and `test_timeout_is_retryable` — two
   separate tests (not parametrized — a `subprocess.TimeoutExpired` `side_effect` doesn't mix
   cleanly with the `MagicMock()`-result parametrize shape used for the other case) — both
   assert `EXIT_RATE_LIMITED` and that `config.RETRY_EPOCH_FILE` gets written.
6. `test_usage_limit_error_is_retryable` — CLI error text matching `is_usage_limit()` →
   `EXIT_RATE_LIMITED`.
7. `test_unrecognized_error_is_fatal_not_retryable` — novel/unmatched error text → `EXIT_FATAL`,
   documents the judgment call directly in the test.
8. `test_sync_skill_package_only_reextracts_when_source_changed` — build a real tiny zip fixture
   in `tmp_path`, call `sync_skill_package()` twice unchanged (mtime of `.synced_from` stays
   identical — no re-extract), then modify the zip and call again (mtime changes, content
   re-extracted).

All tests must set `config.CLAUDE_WORKSPACE_ROOT` and `config.CHAPTER_PROGRESS_DIR` to
`tmp_path` subfolders via `monkeypatch.setattr` — never touch the real `state/` or
`generation-workspace/` directories.

### `tests/test_dispatch.py`

Replace `test_generate_notes_subprocess_mode_raises` (currently asserts `NotImplementedError`,
now stale) with:
```python
def test_generate_notes_subprocess_mode_dispatches_to_cli_stage2():
    """When STAGE2_IMPL='subprocess', dispatch calls the real (built)
    claude_cli_subprocess.stage2.run_stage2_chapter."""
    with patch('src.agents.dispatch.config') as mock_config:
        mock_config.STAGE2_IMPL = 'subprocess'
        with patch('src.claude_cli_subprocess.stage2.run_stage2_chapter', return_value=0) as mock_s2:
            from src.agents.dispatch import generate_notes
            result = generate_notes(Path('/fake'), live_mode=False, verbose=False)
            assert result == 0
            mock_s2.assert_called_once()
```

### `tests/test_main_cli.py`

Add one test confirming the fail-fast is gone:
```python
def test_stage2_impl_subprocess_no_longer_fails_fast():
    from src.main import build_parser
    parser = build_parser()
    args = parser.parse_args(["--stage2-mode", "off", "--stage2-impl", "subprocess"])
    assert args.stage2_impl == "subprocess"
```

## Templates/config disposition

No changes needed — verified `templates/` currently contains only `study-notes.skill` and
`_archived_/`. `study-notes-skill.md` (the file the original superseded doc flagged as a
removal candidate) doesn't exist in this repo; nothing to delete.

## Verification plan

**Step 1 — resolve the skill-discovery mechanic manually, before trusting any wrapper code:**
```bash
mkdir -p /tmp/skill-smoketest && cd /tmp/skill-smoketest
python3 -m zipfile -e "/mnt/c/06-PROJECTS/trial/study-notes-automation-redesigned/templates/study-notes.skill" ./.claude/skills/study-notes/
env -u ANTHROPIC_API_KEY claude -p "/study-notes list the tools available to you" --output-format json --permission-mode acceptEdits --allowedTools Read,Glob
claude -p --help | grep -A2 -i resume
```
Confirm the skill loads from a project-local path, note the exact JSON envelope field names,
and confirm the `-r`/`--resume` flag spelling. If project-local discovery doesn't work
reliably, the fallback is syncing into `~/.claude/skills/study-notes/` instead — a one-line
change to `config.CLAUDE_SKILL_INSTALL_DIR`, not a design pivot.

**Step 2 — static / zero-invocation checks:**
```bash
cd /mnt/c/06-PROJECTS/trial/study-notes-automation-redesigned
pytest tests/ -q
python3 src/main.py --stage2-mode no-llm --stage2-impl subprocess
python3 src/main.py --doctor
```

**Step 3 — one manual real single-chapter run:**
```bash
python3 src/main.py --stage2-mode llm-full --stage2-impl subprocess
```
Watch the log live. Confirm the scrubbed-env subprocess call fires, `expected_docx.exists()`
gates `MARKER`, cost/usage are logged, and — critically, out-of-band, since this can't be
verified from the project's own logs — that the run billed against the Claude.ai subscription
usage view, not the Anthropic Console.

**Step 4 — induce a resumable failure:**
```bash
CLAUDE_CLI_TIMEOUT_SECONDS=10 python3 src/main.py --stage2-mode llm-full --stage2-impl subprocess
python3 src/main.py --stage2-mode llm-full --stage2-impl subprocess   # re-run: confirm resume
```
Confirm the progress file's `session_id` is picked up and `-r <session_id>` is passed on the
second call.

**Step 5 — capture real error text, tune `classify_cli_result()`:** whenever a real
rate-limit/usage-window error is naturally observed, add its exact text as a new fixture in
`test_claude_cli_subprocess_stage2.py` and adjust pattern matching if needed.

**Step 6 — nightly automation:** only after several sanity-checked manual runs, switch nightly
Task-Scheduler `-PipelineArgs` to `--stage2-impl subprocess` via `docs/setup-guide.md`'s
"Switching the Nightly Pipeline Mode" section. No wrapper-script changes needed.

**Automated commands:**
```bash
pytest tests/test_claude_cli_subprocess_stage2.py -q
pytest tests/test_dispatch.py tests/test_main_cli.py -q
```

## Execution order

1. `config/settings.py` — insert Stage 2 settings block, AND fix the stale line 93 comment.
2. `src/claude_cli_subprocess/stage2.py` — replace stub with full implementation.
3. `src/main.py` — remove fail-fast block, update help text.
4. `tests/test_claude_cli_subprocess_stage2.py` — new file.
5. `tests/test_dispatch.py` — replace the one stale test.
6. `tests/test_main_cli.py` — add one test.
7. `scripts/wsl-study-notes-processor.sh` — update `-h`/`--help` text's Stage 2 subprocess line.
8. `docs/architecture.md` — update the call-graph's `--stage2-impl subprocess` leaf.
9. `docs/cli-subprocess-plan.md` — update the three "scaffolded/build later" framings (Decisions
   section, section header) to reflect Stage 2 is now built.
10. `pytest tests/ -q` — all green, zero live `claude` calls made by the automated suite.
11. Execute the Verification plan's steps 1-5 manually before touching nightly automation.

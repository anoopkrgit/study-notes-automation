"""
func_tools_and_utils.py

A shared TOOLBOX used by every part of this pipeline (Stage 1 file-routing,
Stage 2 note-generation, and all three parallel implementations in
src/direct_api/, src/agents/, src/claude_cli_subprocess/). It has four
unrelated jobs bundled into one file because each one is small and every
other file needs it:

  1. LOGGING -- one shared "logger" object (see setup_logger()) that every
     other file imports and writes progress/warning/error lines through, so
     all output ends up in one consistent, timestamped format.

  2. FILE FINGERPRINTING & PROGRESS TRACKING -- sha256() fingerprints a
     file's exact contents, and load_state()/save_state() persist a JSON
     file on disk recording which files have already been processed, so a
     second run doesn't redo (and re-pay for) work the first run already
     finished. acquire_lock()/release_lock() protect that JSON file from
     two pipeline runs writing to it at the same time.

  3. API ERROR TRIAGE -- classify_api_error() looks at a failed call to
     Anthropic's AI service and decides whether it's worth automatically
     retrying later (e.g. a temporary rate limit) or not (e.g. the account
     ran out of credit, which waiting can never fix).

  4. "TOOLS" FOR THE AI TO CALL -- the functions from tool_read() onward.
     When an AI model is given a list of "tools" (small, named actions with
     a description of what they do), it can choose, on its own, to call one
     mid-conversation instead of just returning text -- e.g. asking to read
     a file, write a file, or run a command -- and gets the tool's result
     fed back into the conversation so it can decide what to do next. This
     is how the AI in this project actually reads source material and
     writes the finished study-notes files: every "tool" below is one
     specific, narrowly-scoped action the AI is allowed to take, and
     nothing else. Each one is deliberately restricted (see tool_bash()) so
     an unattended overnight run can never do more than it's meant to.
"""

import base64
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import settings as config

try:
    import anthropic
    # We only need this import here for its EXCEPTION CLASSES (e.g.
    # `anthropic.RateLimitError`) so classify_api_error() below can tell
    # different kinds of API failures apart with `isinstance(...)`. This
    # file never makes an API call itself.
except ImportError:
    anthropic = None

# Standard Exit Codes
EXIT_OK = 0
EXIT_NOOP = 0
EXIT_FATAL = 1
EXIT_RATE_LIMITED = 42
EXIT_DRIVE_UNAVAILABLE = 10

def setup_logger(name: str = "pipeline") -> logging.Logger:
    """Set up a logger that writes formatted lines to stdout ONLY.

    WHY NOT ALSO A FileHandler WRITING DIRECTLY TO THE LOG FILE: an earlier
    version of this function had one, on top of this stdout handler. That
    seemed reasonable in isolation, but both `wsl-study-notes-processor.sh`
    (WSL) and `win-environment-setup.ps1` (Windows) ALSO capture this
    process's own stdout into that exact same log file (`exec >>"$LOG_FILE"
    2>&1` on the bash side) -- so every single line was being written to
    the file TWICE: once by this module's own FileHandler, and once again
    via the wrapper's redirection of the same stdout the StreamHandler
    below already writes to. That doubled every log line, over the entire
    log (caught by actually running the deployed wrapper end-to-end, not
    just reading the code -- the duplication wasn't visible from either
    file in isolation).
    stdout-only here is not a loss of coverage: the wrapper's blanket
    redirect already captures both these formatted messages AND any raw,
    unhandled Python traceback (which bypasses this logging setup
    entirely, going straight to stderr) -- something a FileHandler here
    could never have captured anyway. The one thing you lose is a log FILE
    when running e.g. `python3 src/main.py --doctor` directly at a terminal
    with no wrapper involved; the terminal itself still shows everything,
    and `python3 src/main.py --doctor | tee -a some_file.log` covers it if
    you ever want a copy from a standalone run.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger

logger = setup_logger()

class TokenTracker:
    """Accumulates per-model API call counts and token usage for one stage
    run, so the log can report which model(s) were actually used and how
    many tokens each spent -- e.g. Stage 1 routing calls Haiku, Stage 2
    generation calls a different model entirely, and neither was
    previously visible in the log at all."""

    def __init__(self):
        self.by_model = {}

    def record(self, model: str, usage) -> None:
        """`usage` is an Anthropic response's `.usage` object (or None,
        e.g. a failed/skipped call -- silently ignored)."""
        if usage is None or not model:
            return
        d = self.by_model.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        d["calls"] += 1
        d["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
        d["output_tokens"] += getattr(usage, "output_tokens", 0) or 0

    def log_summary(self, log: logging.Logger, stage_label: str) -> None:
        if not self.by_model:
            log.info(f"{stage_label} token usage: no API calls made.")
            return
        log.info(f"{stage_label} token usage summary:")
        total_calls = total_in = total_out = 0
        for model, d in self.by_model.items():
            log.info(f"  model={model}  calls={d['calls']}  input_tokens={d['input_tokens']}  output_tokens={d['output_tokens']}")
            total_calls += d["calls"]
            total_in += d["input_tokens"]
            total_out += d["output_tokens"]
        log.info(f"  TOTAL  calls={total_calls}  input_tokens={total_in}  output_tokens={total_out}")

def sha256(path: Path) -> str:
    """Compute a SHA-256 "fingerprint" of a file's exact contents.

    A hash function reads a file's bytes and produces a short, fixed-length
    string (the "hash") that is effectively unique to those exact bytes --
    change even one character in the file and the hash comes out completely
    different. This lets the pipeline answer "has this file changed since
    last time?" by comparing hashes instead of comparing entire file
    contents (or trusting the file's last-modified timestamp, which cloud
    sync tools can reset even when nothing actually changed). The state
    file (see load_state()/save_state()) stores each processed file's hash
    so already-handled files aren't re-processed (and, for LLM calls,
    re-paid-for) on the next run.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

# The pipeline's OWN marker/manifest files (e.g. "_sources.txt", the
# "_hold" flag) live inside chapter folders alongside the real transcripts
# and supporting material. They must never be treated as "real input" --
# without this, Stage 2 would list _sources.txt to the generator model as
# if it were one of the chapter's own class transcripts (caught during
# testing: a sandboxed run showed "_sources.txt" listed under "Spine
# Transcripts" in the generation prompt).
_PIPELINE_META_NAMES = {config.MARKER, config.HOLD, config.FAILMARK, config.SOURCES, config.NEWMAT}


def is_ignorable(p: Path) -> bool:
    """Check if file should be ignored (OS/cloud-sync metadata, this
    pipeline's own marker/manifest files, dotfiles, disallowed extensions,
    or an empty file)."""
    return (p.name in config.IGNORE_NAMES or p.name in _PIPELINE_META_NAMES
            or p.suffix.lower() in config.IGNORE_EXTS
            or p.name.startswith(".") or p.stat().st_size == 0)

def acquire_lock(timeout: int = 60) -> bool:
    """Claim exclusive permission to read or write the shared state file.

    A "lock" here is simply a marker folder on disk: if it doesn't exist,
    creating it succeeds and this function returns True, meaning "you now
    have exclusive access." If it already exists, someone else (another
    pipeline run) got there first, and this function waits and retries
    (checking every 2 seconds) until either the lock is free or `timeout`
    seconds pass, at which point it gives up and returns False. This
    prevents two pipeline runs happening at once from both reading the
    state file, both making changes, and then one run's save overwriting
    (silently erasing) the other's -- a classic "lost update" bug. If a
    lock is found to be older than 5 minutes, it's assumed to be left over
    from a run that crashed without cleaning up after itself, and is
    forcibly removed so the pipeline doesn't get stuck waiting forever.
    """
    lock_dir = config.STATE_FILE.with_suffix(".lockdir")
    start = time.time()
    while time.time() - start < timeout:
        try:
            lock_dir.mkdir(parents=True, exist_ok=False)
            return True
        except FileExistsError:
            # Auto-break stale locks older than 5 minutes
            try:
                lock_age = time.time() - lock_dir.stat().st_mtime
                if lock_age > 300:  # 5 minutes
                    logger.warning(f"Breaking stale lock (age {lock_age:.0f}s > 300s)")
                    release_lock()
                    continue
            except OSError:
                pass
            time.sleep(2)
    logger.error(f"Failed to acquire state lock after {timeout}s")
    return False

def release_lock():
    """Give up exclusive access to the state file (delete the lock marker
    folder created by acquire_lock()), so the next run -- or a run that was
    waiting -- can claim it. Safe to call even if the lock is already gone
    (the OSError from a missing folder is simply ignored)."""
    lock_dir = config.STATE_FILE.with_suffix(".lockdir")
    try:
        lock_dir.rmdir()
    except OSError:
        pass

def load_state() -> dict:
    """Load assembler state file.

    IMPORTANT about the lock: `acquire_lock()` returns True/False to say
    whether it actually got the lock. A previous version of this function
    ignored that return value -- it called `acquire_lock()` and just kept
    going regardless of the answer, meaning that if the lock timed out
    (another run was already mid-write), this function would still read the
    file AND then call `release_lock()` -- deleting the OTHER process's lock
    out from under it. That defeats the entire point of having a lock. The
    fix is simple: check the return value, and only ever call
    `release_lock()` in the branch where we know `acquire_lock()` actually
    succeeded (the `try / finally` below only runs once we're past that
    check).
    """
    if not config.STATE_FILE.exists():
        return {"processed": {}}
    if not acquire_lock():
        # Refuse to guess. If we silently returned an empty state here,
        # every already-processed file would look "new" again next run,
        # causing duplicate copies and duplicate (paid) LLM routing calls.
        # Better to fail loudly so whoever is watching the log notices a
        # genuinely stuck lock, rather than the pipeline quietly redoing
        # work.
        raise RuntimeError(
            "Could not acquire the state-file lock (another run may still "
            "be in progress). Refusing to read state without it."
        )
    try:
        return json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"State file unreadable ({e}); starting fresh")
        return {"processed": {}}
    finally:
        release_lock()

def save_state(state: dict, dry: bool = False):
    """Save assembler state file atomically. See load_state()'s docstring for
    why checking acquire_lock()'s return value matters here too."""
    if dry:
        return
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not acquire_lock():
        raise RuntimeError(
            "Could not acquire the state-file lock (another run may still "
            "be in progress). Refusing to save (would risk clobbering its work)."
        )
    try:
        tmp = config.STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(config.STATE_FILE)
    finally:
        release_lock()

# --- API error classification (rate limits, billing, outages) -------------
# WHY THIS EXISTS: this project talks to the Anthropic API directly with an
# API key, billed per token. That is a DIFFERENT billing model from the
# `claude` CLI's Claude Pro/Max SUBSCRIPTION, whose "usage limit hit" errors
# reliably reset on a ~5-hour window. Direct API errors don't all behave
# that way:
#   - "too many requests per minute" (HTTP 429)  -> resets in SECONDS/MINUTES,
#     and the API tells you exactly when via response headers.
#   - "servers are overloaded right now" (HTTP 529) -> transient, unrelated
#     to your account, worth a short retry.
#   - "your account has run out of credit" -> will NEVER resolve itself by
#     waiting. Scheduling a wake-up-and-retry for this would just repeat the
#     same failure every night until a human adds funds.
# So instead of one blanket "assume ~5 hours and try again" rule, this
# function looks at what actually went wrong and decides case by case.
def classify_api_error(exc: Exception) -> dict:
    """Decide what to do after an Anthropic API call raised `exc`.

    Returns a dict with three keys:
      "retry"       (bool) -- should the caller wait and try again later?
      "retry_epoch" (int | None) -- unix timestamp of when to retry, if retry=True
      "reason"      (str)  -- short human-readable explanation, for logging
    """
    now = int(time.time())

    if anthropic is None:
        # The `anthropic` package isn't installed, so we can't even inspect
        # what kind of error this is. Treat it as non-retryable rather than
        # guessing.
        return {"retry": False, "retry_epoch": None,
                "reason": f"anthropic package unavailable; cannot classify error: {exc}"}

    if isinstance(exc, anthropic.RateLimitError):
        # A short-lived "too many requests" limit. The API's response
        # headers say exactly when it's safe to retry -- prefer that over
        # any guess.
        retry_epoch = _extract_retry_epoch(exc) or (now + 60)
        return {"retry": True, "retry_epoch": retry_epoch,
                "reason": f"rate limited (HTTP 429); retry at {retry_epoch}"}

    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        # A network hiccup (DNS, timeout, connection dropped) -- nothing to
        # do with quota or billing. A short retry is enough.
        return {"retry": True, "retry_epoch": now + 300,
                "reason": f"network error ({type(exc).__name__}); retry in 5 min"}

    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", None)
        msg = str(exc).lower()
        if status == 529 or "overloaded" in msg:
            # Anthropic's servers are temporarily overloaded (not specific
            # to this account) -- a modest backoff is enough.
            return {"retry": True, "retry_epoch": now + 600,
                    "reason": "API overloaded (HTTP 529); retry in 10 min"}
        if "credit balance" in msg or "insufficient" in msg or "billing" in msg:
            # Waiting will NOT fix this -- the account needs funds added.
            return {"retry": False, "retry_epoch": None,
                    "reason": f"billing/credit issue, needs human action: {exc}"}
        if status is not None and status >= 500:
            return {"retry": True, "retry_epoch": now + 600,
                    "reason": f"server error (HTTP {status}); retry in 10 min"}
        # Anything else (bad request, bad auth, permission denied, a
        # malformed prompt, ...) is almost certainly a configuration bug
        # that waiting will not fix.
        return {"retry": False, "retry_epoch": None,
                "reason": f"non-retryable API error (HTTP {status}): {exc}"}

    # Not an Anthropic SDK error at all -- an unexpected bug elsewhere in
    # our own code. Don't swallow it as if it were a quota problem.
    return {"retry": False, "retry_epoch": None, "reason": f"unexpected error: {exc}"}


def _extract_retry_epoch(exc: Exception):
    """Best-effort: read a retry time out of an API error's HTTP response
    headers. Returns a unix timestamp, or None if nothing usable was found
    (the caller then falls back to a fixed default wait)."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    now = int(time.time())
    # 1) The standard HTTP header: "retry after this many seconds".
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return now + max(1, int(float(retry_after)))
        except ValueError:
            pass
    # 2) Anthropic-specific headers give an absolute reset time (ISO 8601
    #    text, e.g. "2026-07-25T20:15:00Z") for requests-per-minute /
    #    tokens-per-minute limits specifically.
    for key in ("anthropic-ratelimit-requests-reset", "anthropic-ratelimit-tokens-reset"):
        val = headers.get(key)
        if val:
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return int(dt.timestamp())
            except ValueError:
                continue
    return None


def write_retry_epoch(retry_epoch: int):
    """Write the unix timestamp the pipeline should be woken up and retried
    at, so the Windows wrapper (win-environment-setup.ps1) can read it and
    register a one-time wake task. Mirrors config.RETRY_EPOCH_FILE."""
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.RETRY_EPOCH_FILE.write_text(str(int(retry_epoch)), encoding="utf-8")


# --- Agentic Tools Implementation ---
# Everything below is a "tool": a small Python function the AI model is told
# about (name, plain-English description, and what arguments it takes) and
# may choose to call, by name, while it works through a task -- the same way
# a person might use a text editor or a search box. The AI never runs this
# Python code directly; instead it asks ("please call tool_read with
# path=X"), the surrounding code (see src/direct_api/stage2_api.py and
# src/agents/base.py) actually calls the matching function here, and the
# function's RETURN VALUE (a plain string, in most of these) is handed back
# to the AI as the result, so it can decide what to do next. Every function
# in this section returns a human-readable status/error string rather than
# raising an exception on failure, on purpose -- an exception would crash
# the whole pipeline run, whereas a returned error string lets the AI see
# what went wrong and try something else, the same way a person would read
# an error message and adjust.

def tool_read(path: str) -> str:
    """Tool: read and return a text file's entire contents, so the AI can
    see what's inside a source document or a file it wrote earlier."""
    p = Path(path)
    if not p.exists():
        return f"Error: File '{path}' not found."
    return p.read_text(encoding="utf-8", errors="replace")

def tool_write(path: str, content: str) -> str:
    """Tool: create (or completely overwrite) a text file with the given
    content, creating any missing parent folders first. This is how the AI
    actually produces its output files on disk, e.g. content.json."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Successfully wrote {len(content)} characters to {path}"

def tool_edit(path: str, old_string: str, new_string: str) -> str:
    """Tool: make a small, targeted change to an existing file by finding
    one exact snippet of text (`old_string`) and replacing just its FIRST
    occurrence with `new_string`, leaving the rest of the file untouched.
    This is safer and cheaper than tool_write() for a one-line fix, since it
    doesn't require the AI to re-supply the entire file's contents just to
    change a few words. Fails with an error string (rather than guessing)
    if `old_string` isn't found verbatim in the file."""
    p = Path(path)
    if not p.exists():
        return f"Error: File '{path}' not found."
    text = p.read_text(encoding="utf-8")
    if old_string not in text:
        return f"Error: Target string not found in {path}"
    new_text = text.replace(old_string, new_string, 1)
    p.write_text(new_text, encoding="utf-8")
    return f"Successfully replaced content in {path}"

def tool_glob(pattern: str, base_dir: str = ".") -> str:
    """Tool: list every file under `base_dir` whose name matches a wildcard
    `pattern` (e.g. "*.pdf" for every PDF, "**/*.json" for every JSON file
    in any subfolder), so the AI can discover what files actually exist
    without having to guess exact names. Returns the list as JSON text."""
    p = Path(base_dir)
    matches = [str(m) for m in p.glob(pattern)]
    return json.dumps(matches, indent=2)

def tool_grep(pattern: str, file_path: str) -> str:
    """Tool: search one file's text for every place matching a "regular
    expression" `pattern` (a compact mini-language for describing text
    patterns, e.g. "Chapter \\d+" matches "Chapter" followed by any number)
    and return every match found, as JSON text. Lets the AI find something
    specific inside a large file without reading the whole thing."""
    p = Path(file_path)
    if not p.exists():
        return f"Error: File '{file_path}' not found."
    text = p.read_text(encoding="utf-8", errors="replace")
    import re
    matches = re.findall(pattern, text)
    return json.dumps(matches, indent=2)

ALLOWED_BASH_COMMANDS = {"python3", "python", "node", "soffice", "mkdir", "ls", "cat", "cp", "mv"}

# These coreutils are on PATH on Linux/macOS but have NO executable on native
# Windows (they are shell/cmd builtins or simply absent), so shelling out to
# them via subprocess raises FileNotFoundError there. On Windows we service
# them directly in Python instead (see _run_windows_builtin), so tool_bash
# behaves identically on every OS. `soffice`/`python`/`node` are real .exe's
# on Windows PATH and continue to run through subprocess.
_WINDOWS_BUILTIN_COMMANDS = {"ls", "cat", "cp", "mv", "mkdir"}
_IS_WINDOWS = platform.system() == "Windows"


def _run_windows_builtin(args: list, cwd: str) -> str:
    """Emulate the handful of Unix coreutils (ls/cat/cp/mv/mkdir) that have no
    executable on native Windows, using pure Python so tool_bash works there.

    `args` is the already-shlex-split, validated command (args[0] is the tool
    name). Relative paths are resolved against `cwd`, mirroring how subprocess
    would run the real binary. Returns the same kind of text status string
    tool_bash returns for the subprocess path."""
    name, rest = args[0], args[1:]
    base = Path(cwd)

    def _resolve(p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else base / pp

    try:
        if name == "ls":
            targets = [a for a in rest if not a.startswith("-")]
            paths = [_resolve(t) for t in targets] or [base]
            lines = []
            multi = len(paths) > 1
            for path in paths:
                if not path.exists():
                    lines.append(f"ls: cannot access '{path}': No such file or directory")
                    continue
                if path.is_dir():
                    if multi:
                        lines.append(f"{path}:")
                    lines.extend(sorted(os.listdir(path)))
                else:
                    lines.append(str(path))
            out = "\n".join(lines)
            return out if out.strip() else "Command executed cleanly (no output)."

        if name == "cat":
            files = [a for a in rest if not a.startswith("-")]
            if not files:
                return "Error: cat expects one or more file paths."
            chunks = []
            for f in files:
                path = _resolve(f)
                if not path.exists():
                    return f"cat: {path}: No such file or directory"
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            out = "".join(chunks)
            return out if out.strip() else "Command executed cleanly (no output)."

        if name == "cp":
            recursive = any(a in ("-r", "-R", "--recursive") for a in rest)
            operands = [a for a in rest if not a.startswith("-")]
            if len(operands) != 2:
                return "Error: cp expects exactly SRC and DST (flags aside)."
            src, dst = _resolve(operands[0]), _resolve(operands[1])
            if src.is_dir():
                if not recursive:
                    return f"cp: -r not specified; omitting directory '{src}'"
                shutil.copytree(src, dst / src.name if dst.is_dir() else dst)
            else:
                shutil.copy2(src, dst)
            return "Command executed cleanly (no output)."

        if name == "mv":
            operands = [a for a in rest if not a.startswith("-")]
            if len(operands) != 2:
                return "Error: mv expects exactly SRC and DST."
            src, dst = _resolve(operands[0]), _resolve(operands[1])
            shutil.move(str(src), str(dst))
            return "Command executed cleanly (no output)."

        if name == "mkdir":
            parents = any(a in ("-p", "--parents") for a in rest)
            dirs = [a for a in rest if not a.startswith("-")]
            if not dirs:
                return "Error: mkdir expects at least one directory path."
            for d in dirs:
                _resolve(d).mkdir(parents=parents, exist_ok=parents)
            return "Command executed cleanly (no output)."
    except OSError as e:
        return f"Execution error: {e}"

    return f"Error: Command '{name}' is not in the allowed command list."


def tool_bash(command: str, cwd: str = ".") -> str:
    """Execute ONE restricted, single command (no shell chaining) with
    security sanitization.

    WHY THIS IS DELIBERATELY RESTRICTIVE: the generator model runs
    unattended overnight with no human watching, so this tool must NEVER
    let it run arbitrary shell (that would mean "run any command as your
    user account" via a tool the model itself decides to call). Only a
    fixed list of known-safe program names are allowed, and shell
    metacharacters (pipes, chaining, redirection, command substitution)
    are rejected outright -- see `dangerous_chars` below.

    A CONSEQUENCE of that restriction: multi-step pipelines like
    "soffice --convert-to pdf FILE.svg | pdftoppm -png" (the diagram
    conversion pipeline the study-notes skill documents) CANNOT run
    through this tool, since it contains a `|`. That pipeline has its own
    dedicated tool instead -- see tool_convert_to_png() below -- which
    performs the same two steps safely in Python, without needing shell
    chaining at all.
    """
    import shlex
    # Reject shell metacharacters that could allow command chaining
    dangerous_chars = {';', '&&', '||', '|', '`', '$(', '>', '<', '\n'}
    for dc in dangerous_chars:
        if dc in command:
            return f"Error: Command contains disallowed shell metacharacter '{dc}'."
    try:
        args = shlex.split(command)
    except ValueError as e:
        return f"Error: Could not parse command: {e}"
    if not args:
        return "Error: Empty command."
    if args[0] not in ALLOWED_BASH_COMMANDS:
        return f"Error: Command '{args[0]}' is not in the allowed command list."
    # On native Windows the Unix coreutils have no executable to shell out to,
    # so service them in Python instead (identical result, no subprocess).
    if _IS_WINDOWS and args[0] in _WINDOWS_BUILTIN_COMMANDS:
        return _run_windows_builtin(args, cwd)
    try:
        res = subprocess.run(args, shell=False, cwd=cwd, capture_output=True, text=True, timeout=300)
        out = res.stdout + ("\nSTDERR:\n" + res.stderr if res.stderr else "")
        return out if out.strip() else "Command executed cleanly (no output)."
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 300 seconds."
    except Exception as e:
        return f"Execution error: {e}"


# --- Visual tools (diagram conversion + actually SEEING images/PDF pages) --
# WHY THESE EXIST: the study-notes skill requires a "diagram QA" pass
# (checking arrowheads exist, labels don't overlap, etc.) and a "convert to
# PDF and inspect EVERY page" accuracy pass. Both of those are fundamentally
# visual checks -- there is no way to do them by reading text. An earlier
# version of this tool set only had `tool_read()`, which reads plain text
# and cannot represent an image at all, making those two skill requirements
# impossible to actually perform. The functions below fix that using the
# Anthropic API's "image" content block type, which lets the model literally
# see a picture, the same way Claude Code's built-in Read tool can.

def _text_block(text: str) -> list:
    """Wrap a plain string as the one-item content-block list format the
    Anthropic API expects for a tool result."""
    return [{"type": "text", "text": text}]


def _image_block_from_bytes(data: bytes, media_type: str) -> list:
    """Wrap raw image bytes as an 'image' content block, base64-encoded
    (the API only accepts images as base64 text inside JSON, never raw
    binary -- see also extract_content() in stage1_api.py,
    which does the same thing for PDFs)."""
    encoded = base64.standard_b64encode(data).decode("ascii")
    return [{"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}}]


_IMAGE_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                      ".jpeg": "image/jpeg", ".webp": "image/webp"}


def tool_view_image(path: str) -> list:
    """Return an image file's actual pixels as a viewable content block, so
    the model can visually inspect a diagram it (or a previous turn) drew --
    e.g. to check arrowheads are present and labels don't overlap, per the
    skill's diagram-QA checklist."""
    p = Path(path)
    if not p.exists():
        return _text_block(f"Error: File '{path}' not found.")
    media_type = _IMAGE_MEDIA_TYPES.get(p.suffix.lower())
    if media_type is None:
        return _text_block(f"Error: unsupported image type '{p.suffix}' "
                            f"(expected one of: {', '.join(_IMAGE_MEDIA_TYPES)}).")
    try:
        return _image_block_from_bytes(p.read_bytes(), media_type)
    except OSError as e:
        return _text_block(f"Error reading '{path}': {e}")


def tool_view_pdf_page(path: str, page: int) -> list:
    """Render ONE page of a PDF to an image and return it as a viewable
    content block, so the model can visually proofread that specific page --
    needed for the skill's "convert to PDF and inspect EVERY page" accuracy
    check (checking every page one at a time is exactly what this enables).
    Internally shells out to `pdftoppm` (same tool the conversion pipeline
    uses), rendered into a throwaway temporary folder that's cleaned up
    automatically once this function returns."""
    p = Path(path)
    if not p.exists():
        return _text_block(f"Error: File '{path}' not found.")
    if page < 1:
        return _text_block("Error: page numbers start at 1.")
    with tempfile.TemporaryDirectory() as tmp_dir:
        prefix = str(Path(tmp_dir) / "page")
        try:
            subprocess.run(
                ["pdftoppm", "-f", str(page), "-l", str(page), "-r", "150", "-png", str(p), prefix],
                check=True, capture_output=True, timeout=60,
            )
        except FileNotFoundError:
            return _text_block("Error: 'pdftoppm' is not installed / not on PATH.")
        except subprocess.CalledProcessError as e:
            return _text_block(f"Error: pdftoppm failed: {e.stderr.decode(errors='replace')[:500]}")
        except subprocess.TimeoutExpired:
            return _text_block("Error: pdftoppm timed out after 60s.")
        produced = sorted(Path(tmp_dir).glob("page-*.png"))
        if not produced:
            return _text_block(f"Error: page {page} was not produced -- does the PDF have that many pages?")
        return _image_block_from_bytes(produced[0].read_bytes(), "image/png")


def tool_convert_to_png(svg_path: str, dpi: int = 200) -> str:
    """Convert a matplotlib-generated SVG diagram to a PNG file saved next to
    it, via the two-step pipeline the study-notes skill documents:
    `soffice --headless --convert-to pdf` then `pdftoppm -r <dpi> -png`
    (cairosvg/rsvg-convert are noted in the skill as often unavailable in
    this environment, which is why this pipeline is used instead).

    This exists as its OWN tool (rather than something the model runs
    through tool_bash) because that two-step pipeline needs to run soffice
    and then feed ITS OUTPUT PATH into pdftoppm -- exactly the kind of
    multi-step chaining tool_bash's shell-metacharacter restriction
    deliberately blocks. Doing it here in Python (one subprocess call per
    step, each with its own explicit argument list, no shell involved at
    all) gets the same result safely.

    Returns a plain text status message (not an image) -- the point of this
    tool is producing a PNG file to embed in the .docx. Call
    tool_view_image() afterwards if you want to visually check the result.
    """
    src = Path(svg_path)
    if not src.exists():
        return f"Error: File '{svg_path}' not found."
    if src.suffix.lower() != ".svg":
        return f"Error: expected a .svg file, got '{src.suffix}'."
    out_dir = src.parent
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp_dir, str(src)],
                check=True, capture_output=True, timeout=120,
            )
            pdf_path = Path(tmp_dir) / (src.stem + ".pdf")
            if not pdf_path.exists():
                return "Error: soffice ran but did not produce a PDF."
            subprocess.run(
                ["pdftoppm", "-r", str(dpi), "-png", str(pdf_path), str(out_dir / src.stem)],
                check=True, capture_output=True, timeout=60,
            )
    except FileNotFoundError as e:
        return f"Error: a required tool is not installed / not on PATH: {e}"
    except subprocess.CalledProcessError as e:
        return f"Error: conversion failed: {e.stderr.decode(errors='replace')[:500]}"
    except subprocess.TimeoutExpired:
        return "Error: conversion timed out."
    produced = sorted(out_dir.glob(f"{src.stem}-*.png")) or sorted(out_dir.glob(f"{src.stem}.png"))
    if not produced:
        return "Error: pdftoppm did not produce a PNG file."
    return f"Converted successfully: {produced[0]}"

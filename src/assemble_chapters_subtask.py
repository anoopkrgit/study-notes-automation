#!/usr/bin/env python3
"""
assemble_chapters.py

Front-of-the-nightly bridge for the study-notes pipeline. It POPULATES per-chapter
folders under AI-Chapter-Notes/ from two read-only sources, so the nightly notes
generator finds each chapter's transcript(s) + relevant extra material already
grouped together:

  Stage A — transcripts   (Lecture-Downloads/, canonically named)
      Group PCM transcripts by (Subject, Ch-N) via the downloader's taxonomy
      (readiness/hold stay filename-based). An LLM CONTENT ROUTER then reads each
      new transcript to CONFIRM its filename placement; on agreement it is filed
      as the chapter SPINE, on a confident disagreement it is parked in
      _needs_review/ (the spine is never silently relocated), and if the router
      is unavailable it falls back to filename placement.

  Stage B — supporting content   (Collected-Study-Materials/, generic names)
      The SAME content router reads each new file and returns the (Subject, Ch-N)
      bucket(s) it helps; it is copied into each chapter's supporting/ subfolder
      (multi-chapter files go to EACH). Low-confidence -> _needs_review/ with a
      .why.txt; a chapter it can't place is never force-fit.

  The router calls the local `claude` CLI headless (no SDK / API key) with
  --json-schema for validated output, on a cheap model (Haiku) escalating one
  file to a sharper model (Sonnet) only when the cheap pass is unsure. --no-llm
  reproduces the earlier deterministic (filename-only) routing for both stages.

Design rules (locked with the user):
  • Sources are READ-ONLY. Files are only ever COPIED, never moved/renamed/deleted.
  • Incremental: a sha256 state map means each run processes only NEW files.
  • A file is recorded as processed ONLY after it lands somewhere, so a mid-run
    usage-limit just leaves the rest "new" for next time (fail-safe, no resume
    logic needed).
  • Readiness gate: a chapter folder carries a "_hold" marker (picker skips it)
    until the chapter looks complete — a higher chapter has started for that
    subject, OR its latest lecture is older than IDLE_DAYS. Supporting content
    may pre-seed a held folder before its transcript arrives; that's harmless.
  • Regeneration: a NEW transcript on an already-generated chapter archives the
    old .docx to _prev/ and clears the done-marker so it rebuilds. NEW supporting
    content only drops a _new-material.txt flag (no rebuild).

Usage:
  python3 assemble_chapters.py [--dry-run] [--no-llm] [--verbose]

────────────────────────────────────────────────────────────────────────────────
READER'S NOTE (for non-Python-experts): this file is commented more heavily than
typical production Python, on purpose, so someone who codes but doesn't know
Python's idioms can follow it line by line. Python-specific quirks (comprehensions,
truthy/falsy defaults, tuple unpacking, f-strings, etc.) are explained the first
time they appear and referenced more briefly afterwards.
────────────────────────────────────────────────────────────────────────────────
"""

# ── Imports ──────────────────────────────────────────────────────────────────
# Python's standard library ships all of these — no third-party packages needed
# for this file (unlike assemble_chapters_api.py, which needs the `anthropic` SDK).
import argparse    # parses command-line flags like --dry-run / --no-llm / --verbose
import hashlib     # for sha256() content-hashing, used to detect "have we already
                    # seen this exact file's bytes before?" regardless of filename
import importlib   # lets you import a module by name computed at runtime (a string),
                    # instead of a hardcoded `import foo` statement — used below to
                    # pull in a sibling project's code from a non-standard location
import json         # read/write JSON text — used for the on-disk state file and for
                    # building/parsing the LLM's structured JSON response
import re           # regular expressions — pattern matching/replacing in text
import shutil       # higher-level file operations (copy a file, move a file) built
                    # on top of the lower-level `os` module
import subprocess   # lets Python launch another program (here: the `claude` CLI) and
                    # capture what it prints, like running a command in a terminal
import sys          # access to interpreter internals — here, sys.path (see below)
                    # and sys.exit() to set the process's exit code
from datetime import date, datetime  # `date` = calendar date only (no time-of-day);
                    # `datetime` = date + time. Both are classes imported directly so
                    # you can write `date.today()` instead of `datetime.date.today()`.
from pathlib import Path
# `Path` is Python's modern, object-oriented way to represent filesystem paths —
# nicer than plain strings. Notably, the `/` operator is OVERLOADED (redefined) on
# Path objects to mean "join a path segment", e.g. `Path("a") / "b"` gives you the
# path "a/b" (or "a\b" on Windows) without string concatenation or worrying about
# separators. You'll see this all over the file: `TARGET_ROOT / "Setup" / "..."`.

# ── Paths (edit here if the layout moves) ──────────────────────────────────────
TRANSCRIPT_SRC = Path("/mnt/g/My Drive/Education/Student/Lecture-Downloads")
COLLECTED_SRC  = Path("/mnt/g/My Drive/Education/Student/Study-Programme/Collected-Study-Materials")
TARGET_ROOT    = Path("/mnt/g/My Drive/Education/Student/Study-Programme/AI-Chapter-Notes")
CLASSIFY_DIR   = Path("/mnt/g/My Drive/Education/Student/Lecture-Downloads/scripts")

STATE_FILE = TARGET_ROOT / "Setup" / ".assemble_state.json"
LOG_FILE   = Path.home() / "assemble-chapters.log"   # Path.home() = the user's home dir
REVIEW_DIR = TARGET_ROOT / "_needs_review"

# ── Tunables ───────────────────────────────────────────────────────────────────
# (This `import` sits mid-file rather than up in the main import block at the top
# — that's just a pre-existing quirk of this file, not a Python requirement;
# imports are allowed anywhere and take effect as soon as that line executes.)
import os
# `os.environ` behaves like a dict (hashmap) of the process's environment variables.
# `.get(key, default)` is dict's "safe lookup" method: return the value if the key
# exists, else return `default` instead of raising an error. This means these three
# settings can be overridden from the shell (e.g. `CLAUDE_BIN=/other/path python3 ...`)
# without editing the script, but fall back to sensible defaults otherwise.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
IDLE_DAYS  = 14          # a chapter idle this long counts as complete
CONF_MIN   = 0.55        # min LLM confidence to file automatically

# Content routing is a light judgment, so it runs on a cheap model; a single file
# is escalated to the sharper model only when the cheap pass returns no confident
# match. Overridable via env for testing.  (These call the local `claude` CLI
# headless — no SDK, no API key — see run_router.)
ROUTER_MODEL   = os.environ.get("ASSEMBLE_ROUTER_MODEL", "claude-haiku-4-5")
ESCALATE_MODEL = os.environ.get("ASSEMBLE_ESCALATE_MODEL", "claude-sonnet-5")
# A transcript is filed as spine in its filename chapter unless the content router
# is at least this confident it belongs to a DIFFERENT chapter (then -> review).
DISAGREE_MIN   = 0.80

# ── Marker / meta filenames (kept in sync with study-notes-run.sh) ─────────────
HOLD    = "_hold"                 # picker skips a folder holding this
MARKER  = "_notes_done"
FAILMARK = "_notes_FAILED.txt"
SOURCES = "_sources.txt"
NEWMAT  = "_new-material.txt"
SUP_DIR = "supporting"
# `{ ... }` with no `:` between items is a SET literal, not a dict — an unordered
# collection with no duplicate values. Sets are used here purely for fast
# membership tests (`x in IGNORE_NAMES`), which is an O(1) hashmap-style lookup,
# versus an O(n) scan you'd get checking membership in a list.
IGNORE_NAMES = {"desktop.ini", "Thumbs.db", ".DS_Store"}
IGNORE_EXTS  = {".tmp", ".part", ".crdownload"}

# ── Import the downloader's classifier as the single source of truth ───────────
# This block dynamically imports a Python file that lives in a DIFFERENT project
# folder (the Telegram downloader's `scripts/` dir), rather than duplicating its
# filename-parsing logic here. Two steps:
#   1. `sys.path` is the list of directories Python searches when you `import`
#      something. Inserting CLASSIFY_DIR at position 0 (the front) makes that
#      folder searched FIRST, so the module below can be found even though it's
#      outside this project. `str(CLASSIFY_DIR)` converts the Path object to a
#      plain string, since sys.path expects strings.
#   2. `importlib.import_module("name")` is the dynamic equivalent of writing
#      `import name` — used here because the module's location was only just
#      computed at runtime (via CLASSIFY_DIR), so a static `import` statement
#      couldn't reach it. The loaded module is bound to the short alias `cr`,
#      and its functions are called throughout this file as `cr.something(...)`.
sys.path.insert(0, str(CLASSIFY_DIR))
cr = importlib.import_module("_func_classify_and_rename")

# A plain list used as an in-memory log buffer; every line printed via log() is
# also appended here so flush_log() can write them all to disk in one go at the
# end of the run (rather than opening/writing/closing the file on every single
# log call).
_LOG_LINES = []
def log(msg):
    # f-strings (the `f"..."` prefix) let you embed expressions directly inside
    # a string using `{...}`. Here `{datetime.now().isoformat(timespec='seconds')}`
    # calls a method and inserts its return value into the string. `timespec='seconds'`
    # is a keyword argument telling isoformat() to round off to whole seconds
    # instead of including microseconds.
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line)
    _LOG_LINES.append(line)

def flush_log():
    try:
        # `with open(...) as fh:` is a CONTEXT MANAGER — it opens the file, hands
        # you the file handle as `fh`, and GUARANTEES the file gets closed again
        # when the `with` block ends, even if an exception happens inside it.
        # This is the standard, safe way to work with files in Python (versus
        # manually calling open()/close(), which risks leaving files open on error).
        # "a" = append mode (add to the end of the file, don't overwrite it).
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            # `"\n".join(list_of_strings)` is a string method that glues a list of
            # strings together, inserting "\n" (newline) between each pair — the
            # idiomatic way to turn a list of lines into one big multi-line string.
            fh.write("\n".join(_LOG_LINES) + "\n")
    except OSError:
        # Swallow disk/permission errors here specifically (e.g. a transient
        # Google-Drive-mount hiccup) so a logging failure can never crash the
        # actual pipeline — logging is a nice-to-have, not load-bearing.
        pass


# ── Small helpers ──────────────────────────────────────────────────────────────
# `path: Path` and `-> str` are TYPE HINTS. They document the expected argument
# and return types for humans (and editors/linters) but are NOT enforced by
# Python at runtime — you could still pass in the wrong type and Python wouldn't
# stop you. Think of them as machine-checkable comments, not a compiler contract.
def sha256(path: Path) -> str:
    h = hashlib.sha256()   # an incremental hash object — feed it bytes in chunks
    # "rb" = read mode, Binary (raw bytes, not decoded text) — required for hashing,
    # since a hash is computed over the file's exact bytes.
    with open(path, "rb") as fh:
        # `iter(callable, sentinel)` is a two-argument form of Python's built-in
        # `iter()` that's easy to miss if you haven't seen it before: it repeatedly
        # CALLS `callable` (here, the lambda) and yields each result, UNTIL a call
        # returns a value equal to `sentinel` (here, `b""`, an empty bytes value) —
        # at which point iteration stops. So this loop reads the file 1MB at a time
        # until read() returns empty (end of file), without loading the whole file
        # into memory at once. `lambda: fh.read(1 << 20)` is an anonymous inline
        # function (no `def`, no name) that takes no arguments and calls
        # `fh.read(1 << 20)` — `1 << 20` is a bit-shift, a compact way to write
        # 1,048,576 (i.e. 1 MiB), used here as the chunk size in bytes.
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()   # the final hash, as a hex string like "3a7f...9c2e"


def is_ignorable(p: Path) -> bool:
    # pathlib gives you named attributes instead of manual string-splitting:
    #   p.name   -> "file.txt"      (full filename with extension)
    #   p.suffix -> ".txt"          (just the extension, dot included)
    #   p.stem   -> "file"          (name without extension — used elsewhere)
    # `p.stat()` asks the OS for file metadata (size, timestamps, etc.); `.st_size`
    # is the size in bytes. `or` chains multiple conditions and short-circuits:
    # it returns True as soon as ANY one of them is True, without evaluating the
    # rest — so this reads as "ignore this file if EITHER its name is a known junk
    # file, OR its extension is a known partial-download extension, OR its name
    # starts with a dot (hidden file), OR it's literally empty".
    return (p.name in IGNORE_NAMES or p.suffix.lower() in IGNORE_EXTS
            or p.name.startswith(".") or p.stat().st_size == 0)


def target_folder_name(subject_code: str, ch_no: int, ch_name: str) -> str:
    # dict.get(key, default) again — see the os.environ note above. Here it means
    # "look up the full subject name, or just reuse subject_code itself if it's
    # somehow not in the map" (defensive fallback, shouldn't normally trigger).
    full = cr.SUBJECT_FULL.get(subject_code, subject_code)
    name = ch_name.replace("&", "and")
    # `re.sub(pattern, replacement, string)` finds all matches of a regex pattern
    # and replaces them. The `r"..."` prefix makes this a RAW STRING, meaning
    # backslashes are treated literally instead of as escape sequences — regex
    # patterns are full of backslashes, so raw strings avoid a mess of double-
    # escaping. The pattern `[^A-Za-z0-9]+` means "one or more characters that are
    # NOT a letter or digit" (the `^` right after `[` negates the character
    # class), so this collapses any run of punctuation/spaces into a single "-".
    # `.strip("-")` is then chained directly onto the result (method chaining) to
    # trim any leading/trailing "-" left over.
    name = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    return f"{full}-Ch{ch_no}-{name}"


def taxonomy_buckets() -> dict:
    """{(subject_full, ch_no): ch_name} — every valid PCM chapter, canonical names."""
    buckets = {}
    # `.items()` on a dict gives you (key, value) pairs to loop over, and
    # `for code, m in ...` UNPACKS each pair into two variables in one step
    # (equivalent to looping over pairs and writing `code = pair[0]; m = pair[1]`).
    for code, m in cr.load_chapter_maps().items():
        full = cr.SUBJECT_FULL.get(code, code)
        # Nested unpacking: each value from m.items() is itself a pair
        # `(chno, chname)`, so `for _lec, (chno, chname) in m.items()` unpacks
        # BOTH the outer (key, value) pair AND the inner tuple value in one line.
        # The leading underscore in `_lec` is a naming CONVENTION (not special
        # syntax) meaning "this variable exists because unpacking requires it,
        # but we don't actually use its value".
        for _lec, (chno, chname) in m.items():
            # A TUPLE `(full, chno)` is being used here as a dict KEY. This works
            # because tuples are immutable and therefore "hashable" — lists are
            # NOT hashable and could never be used as dict keys this way.
            buckets[(full, chno)] = chname
    return buckets


def is_usage_limit(text: str) -> bool:
    # `text or ""` is a very common Python idiom for defaulting: if `text` is
    # "falsy" (None, or an empty string, or a few other falsy values) then the
    # `or` short-circuits and evaluates to "" instead. This guards against
    # `.lower()` crashing if `text` were None.
    low = (text or "").lower()
    if re.search(r"reached\s*\|\s*\d{9,13}", text or ""):
        # regex: `\s*` = zero-or-more whitespace, `\|` = a literal "|" character
        # (escaped because `|` is normally a regex special character meaning
        # "or"), `\d{9,13}` = a run of 9 to 13 digits (e.g. a Unix timestamp).
        return True
    # `any(condition for item in iterable)` is a GENERATOR EXPRESSION fed into
    # the built-in any() function. It's a compact way to write "loop over each
    # item, check the condition, and return True the moment any one of them is
    # True" (short-circuits — doesn't check the rest once it finds a match). The
    # tuple after `for s in` is just the list of substrings being searched for.
    return any(s in low for s in
               ("usage limit", "rate limit", "5-hour", "limit reached",
                "too many requests", "quota exceeded"))


def folder_is_generated(folder: Path) -> bool:
    if (folder / MARKER).exists():
        return True
    # Path.glob("*.docx") returns a GENERATOR (a lazy iterator) of every path in
    # `folder` matching the wildcard pattern "*.docx" — it doesn't build the full
    # list up front. any(...) over that generator stops at the very first match
    # it finds, so this is efficient even if a folder somehow had many .docx files.
    return any(folder.glob("*.docx"))


def safe_copy(src: Path, dst_dir: Path, dry: bool) -> Path:
    """Copy src into dst_dir; if a different-content file with that name exists,
    disambiguate with a short hash. Returns the destination path."""
    # mkdir(parents=True, exist_ok=True): create every missing directory in the
    # path (parents=True, like `mkdir -p`), and don't raise an error if the
    # directory is already there (exist_ok=True).
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists() and dst.is_file():
        try:
            if sha256(dst) == sha256(src):
                return dst  # already there, identical
        except OSError:
            # If either file couldn't be read/hashed for some transient reason,
            # just fall through to the "disambiguate with a hash suffix" branch
            # below rather than crashing — `pass` here means "do nothing, move on".
            pass
        # String slicing `[:8]` takes the first 8 characters of the hex digest —
        # enough to make a very-unlikely-to-collide short suffix without a full
        # 64-character hash cluttering the filename.
        short = sha256(src)[:8]
        dst = dst_dir / f"{src.stem}__{short}{src.suffix}"
    if not dry:
        # copyfile (data only) — Google Drive's DrvFs mount forbids the utime()
        # that copy2/copystat performs, so we deliberately don't preserve mtime.
        shutil.copyfile(src, dst)
    return dst


def park_for_review(src: Path, why: str, dry: bool) -> Path:
    """Copy a file to _needs_review/ with an adjacent .why.txt. Never force-fits."""
    dst = safe_copy(src, REVIEW_DIR, dry)
    if not dry:
        # `Path.write_text(...)` is a convenience method that opens the file,
        # writes the given string, and closes it again — a shorthand for the
        # `with open(...) as fh: fh.write(...)` pattern used elsewhere in this
        # file, for the common case of "write one string and be done".
        # Also note: two string literals sitting next to each other with nothing
        # between them (`f"{why}\nSource: {src}\n"` followed directly by
        # `"Fix/rename..."`) are automatically concatenated by Python at parse
        # time — no `+` needed. This is just a way to split one long string
        # across two lines in the source code for readability.
        (REVIEW_DIR / (dst.name + ".why.txt")).write_text(
            f"{why}\nSource: {src}\n"
            "Fix/rename and drop back into its source folder to retry.\n",
            encoding="utf-8")
    return dst


# ── LLM content router (headless `claude` CLI — no SDK, no API key) ─────────────
# Structured output is enforced by the CLI's --json-schema flag so we parse a
# validated object instead of scraping JSON out of prose.
# `json.dumps(python_dict)` converts a Python dict (here, a JSON Schema written
# as nested dicts/lists) into a JSON-formatted TEXT string — necessary because
# the CLI flag below expects a raw string argument, not a live Python object.
ROUTER_SCHEMA = json.dumps({
    "type": "object", "additionalProperties": False, "required": ["matches"],
    "properties": {
        "matches": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["subject", "chapter_no", "confidence"],
            "properties": {
                "subject": {"type": "string", "enum": ["Physics", "Chemistry", "Maths"]},
                "chapter_no": {"type": "integer"},
                "role": {"type": "string", "enum": ["spine", "supporting"]},
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
            }}},
        "agrees_with_filename": {"type": ["boolean", "null"]},
    }})


def run_router(prompt: str, model: str):
    """Invoke the headless `claude` CLI with schema-validated output.
    Returns (obj|None, limited: bool) where obj is the parsed schema object.
    (A function that returns MULTIPLE values like this is really returning one
    TUPLE, e.g. `(some_dict, False)` — callers unpack it as `obj, limited = ...`.)
    """
    # `cmd` is a LIST of strings: the program name followed by each argument as
    # its own list element. This is the standard/safe way to build a command for
    # subprocess — Python passes each element straight to the OS without ever
    # interpreting the string as a shell command line, so there's no risk of
    # spaces or special characters in `prompt` being misparsed as extra flags.
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", model,
           "--output-format", "json", "--json-schema", ROUTER_SCHEMA,
           "--dangerously-skip-permissions", "--allowedTools", "Read,Bash"]
    try:
        # subprocess.run(...) launches `cmd` as a child process and waits for it
        # to finish. capture_output=True captures its stdout/stderr into the
        # returned object instead of letting them print straight to the terminal.
        # text=True decodes that output from raw bytes into a normal Python str.
        # cwd=... sets the working directory the child process runs in.
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(TARGET_ROOT))
    except Exception as e:
        # This broad `except Exception` guards against the process failing to
        # even START (e.g. `claude` isn't installed / not on PATH) — a different
        # failure mode from the process running and returning bad output, which
        # is handled further down. `as e` binds the caught exception object to
        # the name `e` so it can be included in the log message.
        log(f"    router call failed to launch: {e}")
        return None, False
    combined = (p.stdout or "") + "\n" + (p.stderr or "")
    if is_usage_limit(combined):
        return None, True
    # Outer envelope: the CLI's --output-format json wraps the run; .result holds
    # the assistant's (schema-constrained) final text.
    result_text = None
    # str.splitlines() breaks a multi-line string into a LIST of individual
    # lines (without the trailing newline characters), so this loop examines
    # the CLI's output one line at a time.
    for line in (p.stdout or "").splitlines():
        line = line.strip()   # remove leading/trailing whitespace from this line
        if line.startswith("{") and '"type"' in line:
            try:
                # json.loads(text) parses a JSON string into Python objects
                # (dicts/lists/etc — the inverse of json.dumps used above).
                # `.get("result", "")` then does a safe dict lookup with a
                # default, same idiom as os.environ.get() earlier.
                result_text = json.loads(line).get("result", "")
            except Exception:
                # This particular line just wasn't valid JSON (e.g. it was some
                # other log line the CLI printed) — ignore it and keep scanning
                # the remaining lines for the one that IS the JSON envelope.
                pass
    if result_text is None:
        result_text = p.stdout or ""
    try:
        return json.loads(result_text), False
    except Exception:
        # schema should prevent this, but stay defensive: pull the first object
        # `re.search(pattern, string, re.S)` finds the first regex match.
        # `re.S` (aka re.DOTALL) makes `.` in the pattern also match newline
        # characters — needed here because `{.*}` (any characters, greedily,
        # between curly braces) must span a JSON blob that could be multi-line.
        m = re.search(r"\{.*\}", result_text, re.S)
        if m:
            try:
                # `m.group(0)` is the full text that matched the regex pattern
                # (group 0 always means "the entire match", as opposed to a
                # parenthesized sub-part of the pattern).
                return json.loads(m.group(0)), False
            except Exception:
                pass
        return None, False


def _parse_matches(obj: dict, buckets: dict):
    """Normalise a router object -> [(subject_full, ch_no, ch_name, role, conf, reason)],
    keeping only buckets that actually exist in the taxonomy."""
    out = []
    # `(obj or {})` again uses the truthy/falsy `or` default idiom: if `obj` is
    # None (the router call failed upstream), fall back to an empty dict so the
    # following `.get(...)` doesn't crash. The whole expression then chains a
    # second `or []` so that even if "matches" is present but explicitly null in
    # the JSON, we still get an empty list to loop over instead of erroring.
    for item in (obj or {}).get("matches", []) or []:
        try:
            full = str(item["subject"]).strip()
            chno = int(item["chapter_no"])
            conf = float(item.get("confidence", 0))
        except (KeyError, TypeError, ValueError):
            # A tuple of exception types after `except` catches ANY of them:
            # KeyError (a required key was missing), TypeError/ValueError (the
            # value existed but couldn't be converted to int/float as expected).
            # `continue` skips straight to the next loop iteration, discarding
            # this malformed item rather than letting one bad entry blow up the
            # whole routing pass.
            continue
        if (full, chno) in buckets:
            # `item.get("role") or None` — if the key is missing OR present-but-
            # empty-string, both are falsy, so this normalises either case to the
            # single value None. `.strip() or ""` on the next line does the
            # mirror-image thing: normalise a missing/None value down to "".
            role = str(item.get("role") or "").strip() or None
            reason = str(item.get("reason") or "").strip()
            out.append((full, chno, buckets[(full, chno)], role, conf, reason))
    return out


def llm_route(path: Path, buckets: dict, prior=None):
    """Content-route ONE file against the fixed taxonomy by reading it.

    prior: optional (subject_full, ch_no, ch_name) filename hint (transcripts).
    Returns (matches, limited, model_used).
      matches = [(subject_full, ch_no, ch_name, role, conf, reason)]
    Runs on the cheap ROUTER_MODEL; escalates one file to ESCALATE_MODEL only when
    the cheap pass yields no confident match.
    """
    # `"\n".join(f"..." for (full, chno), name in sorted(buckets.items()))` is a
    # GENERATOR EXPRESSION (like the `any(...)` ones above, but here its results
    # are collected by `.join()` into one string) that builds one formatted line
    # per taxonomy bucket. `sorted(buckets.items())` sorts the (key, value) pairs
    # so the list is presented to the model in a stable, predictable order.
    tax = "\n".join(f"- {full} | Ch-{chno} | {name}"
                    for (full, chno), name in sorted(buckets.items()))
    prior_line = ""
    if prior:
        prior_line = (f"\nA filename convention suggests this is: {prior[0]} | "
                      f"Ch-{prior[1]} | {prior[2]}. Confirm this from the content, "
                      "or flag disagreement — do not just trust the filename.\n")
    prompt = (
        "You are routing ONE study file for a 9th-grade PCM (Physics/Chemistry/Maths) "
        "course to the chapter(s) whose notes it would actually help build.\n\n"
        f"Read ONLY the first 1-3 pages / title / headings of the file at:\n  {path}\n"
        "Do not read the whole document — read just enough to identify subject + chapter.\n"
        f"{prior_line}\n"
        "Choose from these EXACT (subject, chapter) buckets. A file may span MULTIPLE "
        "chapters — list EVERY bucket that genuinely applies, each with a confidence "
        "0.0-1.0. If nothing fits, return an empty matches list. NEVER invent a bucket "
        "outside this list. Set `role` to \"spine\" only for an actual class lecture "
        "transcript of that chapter, else \"supporting\". Set `agrees_with_filename` to "
        "true/false/null relative to the filename hint above.\n\n"
        f"Valid buckets:\n{tax}"
    )
    obj, limited = run_router(prompt, ROUTER_MODEL)
    if limited:
        return [], True, ROUTER_MODEL
    matches = _parse_matches(obj, buckets)
    # `max(generator_expression, default=0.0)` — this pattern shows up repeatedly
    # in this file, so it's worth explaining once in full: normally `max()` on an
    # EMPTY sequence raises an error ("max() arg is an empty sequence"). The
    # `default=` keyword argument tells it what value to return instead if there
    # was nothing to compare — here, if `matches` is empty, "best confidence
    # found" is defined as 0.0 rather than crashing. `m[4] for m in matches` is a
    # generator expression pulling out just the 5th element (index 4, since
    # indexing starts at 0) — the confidence value — from each match tuple.
    best = max((m[4] for m in matches), default=0.0)
    if best < CONF_MIN:
        obj2, limited2 = run_router(prompt, ESCALATE_MODEL)
        if limited2:
            return [], True, ESCALATE_MODEL
        matches2 = _parse_matches(obj2, buckets)
        if max((m[4] for m in matches2), default=0.0) >= best:
            return matches2, False, ESCALATE_MODEL
    return matches, False, ROUTER_MODEL


def subject_full_to_code(full: str):
    # A plain linear search: loop over every (code, fullName) pair in the
    # SUBJECT_FULL dict and return the code as soon as the full name matches.
    # (There's no reverse-lookup dict built for this because SUBJECT_FULL is
    # small — a handful of entries — so a linear scan is simple and fast enough.)
    for code, f in cr.SUBJECT_FULL.items():
        if f == full:
            return code
    return None   # explicit "not found" signal; falls through if no match


# ── State ──────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log("WARNING: state file unreadable; starting fresh")
    return {"processed": {}}


def save_state(state: dict, dry: bool):
    if dry:
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── Stage A: transcripts ───────────────────────────────────────────────────────
def parse_transcript(name: str):
    """(subject_code, ch_no, ch_name, lec_no, fdate) for a PCM transcript, else None."""
    if cr.classify(name) != "transcript":
        return None
    ch = cr.chapter_from_name(name)
    if not ch:
        return None
    subj, _src = cr.resolve_subject(name)
    if subj not in ("Phy", "Chem", "Maths"):
        return None
    # Returning a 5-element tuple built directly from other calls' results —
    # `ch[0]` / `ch[1]` index into the `ch` tuple returned by chapter_from_name().
    return subj, ch[0], ch[1], cr.detect_lecture_no(name), cr.file_date(name)


def write_sources(folder: Path, entries: list, dry: bool):
    """entries: list of (role, filename, note)."""
    if dry:
        return
    lines = [f"# Chapter sources — regenerated {datetime.now().isoformat(timespec='seconds')}", ""]
    for role, fn, note in entries:   # tuple-unpacking each 3-item entry per loop
        # `(f"  — {note}" if note else "")` is Python's TERNARY / inline-if
        # expression: `<value if condition> else <other value>`. Read it as "if
        # `note` is truthy (non-empty), produce this formatted string; otherwise
        # produce an empty string" — used here to optionally append a note only
        # when one exists, all on one line rather than a separate if/else block.
        lines.append(f"[{role}] {fn}" + (f"  — {note}" if note else ""))
    (folder / SOURCES).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    # argparse builds a command-line interface: it reads sys.argv (the raw
    # command-line arguments the script was invoked with) and turns recognized
    # flags into a tidy object with named attributes (args.dry_run, etc.).
    ap = argparse.ArgumentParser()
    # action="store_true": if the flag is PRESENT on the command line, the
    # resulting attribute is True; if absent, it defaults to False. These are
    # boolean on/off switches, not flags that take a value (like --model=foo).
    ap.add_argument("--dry-run", action="store_true", help="show planned actions, change nothing")
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM classify pass (deterministic + review only)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    dry = args.dry_run

    log("==================================================================")
    log(f"assemble start (dry_run={dry}, no_llm={args.no_llm}, "
        f"router={ROUTER_MODEL}, escalate={ESCALATE_MODEL})")
    # A tuple-of-tuples being looped over and unpacked: each element is
    # `(label, directory)`, unpacked in the `for` header just like elsewhere.
    for label, d in (("transcript src", TRANSCRIPT_SRC), ("collected src", COLLECTED_SRC),
                     ("target root", TARGET_ROOT)):
        if not d.exists():
            log(f"ERROR: {label} not found: {d}")
            # Two statements separated by `;` on one line — purely a style choice
            # equivalent to writing them on two separate lines; both still run
            # in order.
            flush_log(); return 1

    state = load_state()
    processed = state["processed"]
    buckets = taxonomy_buckets()

    # ---- Stage A: group transcripts, compute readiness ----------------------
    groups = {}   # (subj, chno, chname) -> {'files':[Path], 'dates':[date]}
    # Path.iterdir() lists the immediate contents (files AND subfolders) of a
    # directory, as an iterator of Path objects — sorted() here just sorts that
    # into a stable, predictable (alphabetical-ish) order before looping.
    for p in sorted(TRANSCRIPT_SRC.iterdir()):
        if not p.is_file() or is_ignorable(p):
            continue
        parsed = parse_transcript(p.name)
        if not parsed:
            continue
        subj, chno, chname, _lec, fdate = parsed   # tuple unpacking, as before
        key = (subj, chno, chname)
        # dict.setdefault(key, default): if `key` is already in the dict, return
        # its existing value; otherwise INSERT `default` under that key and
        # return it. This is a one-line way to say "get-or-create" without an
        # explicit `if key not in groups: groups[key] = {...}` block first.
        g = groups.setdefault(key, {"files": [], "dates": []})
        g["files"].append(p)
        if fdate:
            g["dates"].append(fdate)

    maxch = {}
    for (subj, chno, _n) in groups:   # looping directly over a dict iterates its KEYS
        # dict.get(key, 0) with a numeric default, then compare with max() — this
        # is the running-maximum idiom: "the highest chapter number seen so far
        # for this subject, or 0 if we haven't seen this subject at all yet".
        maxch[subj] = max(maxch.get(subj, 0), chno)
    today = date.today()

    # A function defined INSIDE another function (a "nested function" / closure).
    # It's only usable within main(), and it can see main()'s local variables
    # (like `groups`, `maxch`, `today`) without them being passed in as arguments.
    def is_ready(key):
        subj, chno, _n = key
        dates = groups[key]["dates"]
        # `max(dates) if dates else None` — the ternary/inline-if again: compute
        # the latest date only if the list is non-empty, else use None so the
        # next line's `is not None` check can safely skip a chapter with no
        # dated files yet instead of crashing on an empty max().
        latest = max(dates) if dates else None
        superseded = chno < maxch.get(subj, 0)
        idle = latest is not None and (today - latest).days > IDLE_DAYS
        # `date - date` (subtracting two `date` objects) gives you a `timedelta`
        # object representing the difference; `.days` extracts that difference
        # as a plain integer number of days.
        return superseded or idle

    # Route-method notes for the manifest, keyed by transcript filename. Seeded from
    # prior runs' state so already-filed transcripts keep their note; overridden with
    # a fresh note for any transcript (re)touched this run.
    # This is a DICT COMPREHENSION: `{key_expr: value_expr for item in iterable if
    # condition}` builds a whole dict in one expression instead of a multi-line
    # loop with `.append()`/assignment. Read it as: "for every record in
    # processed.values() whose 'kind' starts with 'transcript', build an entry
    # mapping that record's filename to its route note."
    route_by_name = {rec.get("name", ""): rec.get("route", "")
                     for rec in processed.values()
                     if str(rec.get("kind", "")).startswith("transcript")}

    # copy new transcripts + maintain folders/hold/regen
    for key in sorted(groups):
        subj, chno, chname = key
        folder = TARGET_ROOT / target_folder_name(subj, chno, chname)
        ready = is_ready(key)
        new_transcript = False
        prior = (cr.SUBJECT_FULL[subj], chno, chname)   # filename hint for the router
        prior_key = (prior[0], chno)
        for src in groups[key]["files"]:
            h = sha256(src)
            if h in processed:
                continue

            # --- content-vet placement (LLM) before filing as spine ------------
            route = "spine (filename)"
            if not args.no_llm:
                matches, limited_a, model_used = llm_route(src, buckets, prior=prior)
                if limited_a:
                    # Deterministic fallback: a correctly-named transcript always
                    # has a safe filename home, so a token outage must not strand it.
                    route = "spine (filename-fallback, router unavailable)"
                    log(f"  [A] router unavailable for {src.name}; filing by filename")
                else:
                    # `any(condition for m in matches)` — a generator expression
                    # feeding any(): True if AT LEAST ONE match tuple's (subject,
                    # chapter) equals prior_key.
                    agrees = any((m[0], m[1]) == prior_key for m in matches)
                    # A LIST COMPREHENSION: `[expr for item in iterable if cond]`
                    # builds an actual list (unlike a generator expression, which
                    # is consumed lazily/once). Here it collects every match that
                    # is BOTH confidently identified AND points to a different
                    # chapter than the filename hint.
                    confident_other = [m for m in matches
                                       if m[4] >= DISAGREE_MIN and (m[0], m[1]) != prior_key]
                    if confident_other and not agrees:
                        # strong disagreement -> park for a human; do NOT relocate the
                        # spine, do NOT desync the filename-based readiness grouping.
                        alt = confident_other[0]
                        why = (f"Transcript placement disagreement: filename says "
                               f"{prior[0]} Ch-{chno} {chname}; content router says "
                               # `{alt[4]:.2f}` is an f-string FORMAT SPEC: the
                               # part after the colon controls how the value is
                               # rendered. `.2f` means "fixed-point decimal,
                               # rounded to exactly 2 digits after the decimal
                               # point" (e.g. 0.8 -> "0.80"). This `:.2f` pattern
                               # recurs throughout the file for confidence scores.
                               f"{alt[0]} Ch-{alt[1]} {alt[2]} (conf {alt[4]:.2f}). "
                               f"Reason: {alt[5] or 'n/a'}")
                        park_for_review(src, why, dry)
                        log(f"  [A] NEEDS REVIEW (placement disagreement): {src.name}")
                        processed[h] = {"src": str(src), "name": src.name,
                                        "kind": "transcript-review", "targets": [],
                                        "route": "review (placement disagreement)",
                                        "ts": datetime.now().isoformat(timespec='seconds')}
                        route_by_name[src.name] = "review (placement disagreement)"
                        # `continue` here skips the rest of THIS iteration of the
                        # innermost `for src in ...` loop and moves on to the next
                        # file, without falling through to the copy-into-folder
                        # code below (this file was parked, not filed).
                        continue
                    # Ternary again: pick a short label depending on which model
                    # actually produced the winning match.
                    tag = "sonnet" if "sonnet" in (model_used or "") else "haiku"
                    # `max(gen, default=max(gen2, default=0.0))` — a max() call
                    # whose OWN default value is itself the result of another
                    # max()-with-default call. Read inside-out: first, try to
                    # find the best confidence among matches that agree with the
                    # filename; if there are none of those, fall back to the
                    # best confidence among ALL matches; if there are no matches
                    # at all, fall back to 0.0.
                    best = max((m[4] for m in matches if (m[0], m[1]) == prior_key),
                               default=max((m[4] for m in matches), default=0.0))
                    route = f"spine (llm-{tag}-confirmed, conf {best:.2f})"

            # regen trigger: new transcript into an already-generated chapter
            if folder_is_generated(folder):
                log(f"  [A] new transcript for GENERATED chapter '{folder.name}' -> archiving old doc, will rebuild")
                if not dry:
                    prev = folder / "_prev"
                    prev.mkdir(exist_ok=True)
                    for docx in folder.glob("*.docx"):
                        shutil.move(str(docx), str(prev / docx.name))
                    for mk in (MARKER, FAILMARK):
                        # Path.unlink(missing_ok=True): delete this file; if it
                        # doesn't exist, don't raise an error (unlike the default
                        # behaviour, which would raise FileNotFoundError).
                        (folder / mk).unlink(missing_ok=True)
            dst = safe_copy(src, folder, dry)
            log(f"  [A] transcript -> {folder.name}/{dst.name}  [{route}]")
            processed[h] = {"src": str(src), "name": src.name, "kind": "transcript",
                            "targets": [folder.name], "route": route,
                            "ts": datetime.now().isoformat(timespec='seconds')}
            route_by_name[src.name] = route
            new_transcript = True

        # refresh manifest + hold state every run (readiness drifts with time)
        if folder.exists() or new_transcript:
            if not dry:
                folder.mkdir(parents=True, exist_ok=True)
            # A LIST COMPREHENSION building tuples, with `sorted(..., key=...)`
            # controlling sort order: `key=lambda x: x.name` tells sorted() to
            # compare items by their `.name` attribute rather than the Path
            # objects themselves (Path objects don't have an obvious natural
            # ordering across OSes the way plain strings do).
            entries = [("transcript", f.name, route_by_name.get(f.name, ""))
                       for f in sorted(groups[key]["files"], key=lambda x: x.name)]
            write_sources(folder, entries, dry)
            hold_path = folder / HOLD
            if ready:
                if hold_path.exists() and not dry:
                    hold_path.unlink()
                # String concatenation with `+` here, combined with a ternary:
                # append the extra note only when new_transcript is True.
                log(f"  [A] chapter READY: {folder.name}" + ("" if not new_transcript else " (new transcript)"))
            else:
                if not dry:
                    hold_path.write_text("held: chapter not yet complete (open or too recent)\n", encoding="utf-8")
                if args.verbose:
                    log(f"  [A] chapter held (not ready): {folder.name}")

    # ---- Stage B: content-route + place supporting material -----------------
    reviewed = placed = 0   # chained assignment: both names start at 0
    for p in sorted(COLLECTED_SRC.iterdir()):
        if not p.is_file() or is_ignorable(p):
            continue
        h = sha256(p)
        if h in processed:
            continue

        # Optional filename hint (collected names are usually generic, so this is
        # rarely present; the router reads content regardless).
        prior = None
        det = cr.chapter_from_name(p.name)
        if det:
            dsubj, _src = cr.resolve_subject(p.name)
            if dsubj in ("Phy", "Chem", "Maths"):
                prior = (cr.SUBJECT_FULL[dsubj], det[0], det[1])

        matches = []
        method = None
        if args.no_llm:
            # deterministic parity (dummy mode): file by filename hint if present,
            # else DEFER — leave unprocessed for a later LLM-enabled run.
            if prior:
                matches = [(prior[0], prior[1], prior[2], "supporting", 0.95, "filename")]
                method = "filename"
        else:
            matches, limited, model_used = llm_route(p, buckets, prior=prior)
            if limited:
                log("  [B] usage limit hit during routing — leaving the rest for next run")
                # `break` exits the entire `for p in sorted(COLLECTED_SRC...)`
                # loop immediately — unlike `continue`, which only skips to the
                # next item, `break` stops the loop altogether. Any remaining
                # unprocessed files are simply left for the next run.
                break
            method = "llm-" + ("sonnet" if "sonnet" in (model_used or "") else "haiku")

        # List comprehension again: keep only matches whose confidence clears
        # the CONF_MIN threshold.
        good = [m for m in matches if m[4] >= CONF_MIN]
        if not good:
            if args.no_llm and method is None:
                if args.verbose:
                    log(f"  [B] deferred (no-llm, no filename match): {p.name}")
                continue
            cand = ", ".join(f"{m[0]} Ch-{m[1]} ({m[4]:.2f})" for m in matches) or "no candidates"
            # `next(generator, default)` pulls the FIRST item out of a generator
            # expression, or returns `default` if the generator produced nothing
            # at all — a concise way to say "give me the first non-empty reason,
            # if there is one" without writing an explicit loop-and-break.
            reason = next((m[5] for m in matches if m[5]), "") if matches else ""
            park_for_review(p, f"Could not confidently route (method={method}). "
                               f"Candidates: {cand}." + (f" Reason: {reason}" if reason else ""), dry)
            log(f"  [B] NEEDS REVIEW: {p.name} (method={method})")
            processed[h] = {"src": str(p), "name": p.name, "kind": "review",
                            "targets": [], "route": f"review ({method})",
                            "ts": datetime.now().isoformat(timespec='seconds')}
            reviewed += 1
            continue

        # A file may help MULTIPLE chapters -> copy into each chapter's supporting/.
        targets, notes = [], []   # two empty lists created in one line
        # Tuple-unpacking each 6-element match tuple directly in the for-header.
        for full, chno, chname, _role, conf, _reason in good:
            code = subject_full_to_code(full)
            folder = TARGET_ROOT / target_folder_name(code, chno, chname)
            dst = safe_copy(p, folder / SUP_DIR, dry)
            targets.append(folder.name)
            notes.append(f"{folder.name}:{conf:.2f}")
            log(f"  [B] supporting -> {folder.name}/{SUP_DIR}/{dst.name} (conf {conf:.2f}, {method})")
            if not dry:
                folder.mkdir(parents=True, exist_ok=True)
                key = (code, chno, chname)
                if key not in groups and not (folder / HOLD).exists() and not folder_is_generated(folder):
                    (folder / HOLD).write_text("held: awaiting transcript for this chapter\n", encoding="utf-8")
                if folder_is_generated(folder):
                    # "a" = append mode again, here via the manual open()/with
                    # pattern (rather than Path.write_text, which would OVERWRITE
                    # the file) because we're adding one more line to a growing
                    # log-like file rather than replacing its whole contents.
                    with open(folder / NEWMAT, "a", encoding="utf-8") as fh:
                        fh.write(f"{datetime.now().isoformat(timespec='seconds')}  new supporting file: {dst.name} "
                                 f"(review whether to regenerate)\n")
                    log(f"      note: chapter already generated — flagged via {NEWMAT}, NOT rebuilding")
        best_reason = next((m[5] for m in good if m[5]), "")
        processed[h] = {"src": str(p), "name": p.name, "kind": "supporting",
                        "targets": targets,
                        "route": f"{method}; " + ", ".join(notes)
                                 + (f" — {best_reason}" if best_reason else ""),
                        "ts": datetime.now().isoformat(timespec='seconds')}
        placed += 1

    save_state(state, dry)
    log(f"assemble end: {placed} supporting placed, {reviewed} to review, "
        f"{len(groups)} chapters tracked" + (" [DRY-RUN: nothing written]" if dry else ""))
    flush_log()
    return 0


# `if __name__ == "__main__":` is Python's standard "only run this if the file
# was EXECUTED directly (e.g. `python3 assemble_chapters.py`), not if it was
# IMPORTED as a module from somewhere else". `__name__` is a special variable
# Python sets automatically: it equals "__main__" for the script that was run,
# but equals the module's own name when that file is instead imported elsewhere
# (like assemble_chapters_api.py does with several of `cr`'s functions' sibling
# module, or like this file's own functions could be imported for testing).
# sys.exit(n) terminates the process with `n` as its exit code (0 = success,
# non-zero = failure) — main()'s return value becomes the process's exit status.
if __name__ == "__main__":
    sys.exit(main())

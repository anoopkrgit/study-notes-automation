"""
stage1_cli.py -- Stage 1 (deciding which chapter a newly-downloaded file
belongs to) via the `claude` command-line program run as a subprocess (a
separate running copy of that program, launched and controlled from here),
billed against a person's Claude subscription instead of the metered
Anthropic API key (see src/claude_cli_subprocess/__init__.py for how this
compares to the other two implementations, src/direct_api/ and src/agents/).

THE ROUTING QUESTION, IN PLAIN TERMS: every incoming study file (a class
transcript, a scanned worksheet, etc.) needs to be filed under the right
Physics/Chemistry/Maths chapter folder before Stage 2 can turn it into
notes. run_router() asks the AI to look at ONE file and say which chapter
"bucket(s)" it belongs to; route_file() is the higher-level function that
builds the question (the "prompt") to ask, decides whether a cheap or a
more careful (and pricier) model should answer it, and turns the AI's raw
answer into the structured result the rest of the pipeline expects.
"""

import json
import re
import subprocess
from pathlib import Path

import settings as config
from src.func_tools_and_utils import logger
from .common import build_claude_env, is_usage_limit

# ROUTER_SCHEMA describes, in a machine-readable format called JSON Schema,
# the EXACT shape the AI's answer must take -- e.g. "a list of matches, each
# with a subject that must be exactly Physics/Chemistry/Maths, a chapter
# number, and a confidence score." Passing this to the `claude` command
# (via --json-schema below) forces the AI's response into that shape,
# instead of free-form prose that this code would then have to guess how to
# parse.
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
    """Launch the `claude` command-line program as a subprocess (a
    separate, independent running copy of that program) with `prompt` as
    its question and `model` as which AI model should answer it, wait for
    it to finish, and parse its answer back into a Python object.

    Returns a (result, hit_usage_limit) pair:
      - On success: (the parsed JSON answer as a dict, False).
      - If the subscription's usage limit was hit: (None, True) -- the
        caller should treat this as "couldn't get a real answer right now"
        rather than "the AI said there were zero matches."
      - On any other failure (crash, bad arguments, unparseable output):
        (None, False) or (None, True) depending on severity -- see the
        comments below at each failure branch for why.
    """
    cmd = [config.CLAUDE_BIN, "-p", prompt, "--model", model,
           "--output-format", "json", "--json-schema", ROUTER_SCHEMA,
           "--dangerously-skip-permissions", "--allowedTools", "Read,Bash"]
    if getattr(config, 'DEV_TOKEN_SAVER_MODE', False):
        # Same cost-safe smoke-test toggle as src/agents/ and src/direct_api/
        # -- the `claude` CLI has no max_tokens flag to cap, so these two
        # native flags are the equivalent: a hard per-call dollar ceiling
        # and a lower reasoning-effort setting. Paired with route_file()'s
        # dummy prompt (skips reading a real file) and skipped escalation.
        cmd += ["--max-budget-usd", str(config.DEV_TOKEN_SAVER_MAX_BUDGET_USD),
                "--effort", config.DEV_TOKEN_SAVER_EFFORT]
    try:
        # Actually run the `claude` program now, as a child process, and
        # wait for it to exit. capture_output=True collects everything it
        # prints (both its normal output and its error output) instead of
        # letting it appear on screen; env=build_claude_env() is what
        # guarantees this call is billed to the subscription, not the API
        # key (see common.py's build_claude_env() docstring).
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(config.LOCAL_RUNTIME_ROOT), env=build_claude_env())
    except Exception as e:
        # The `claude` program itself couldn't even be started (e.g. it
        # isn't installed at the configured path). Nothing to parse.
        logger.error(f"    router call failed to launch: {e}")
        return None, False
    combined = (p.stdout or "") + "\n" + (p.stderr or "")
    if is_usage_limit(combined):
        # The subscription's usage cap was hit -- see is_usage_limit()'s
        # docstring in common.py for why this is treated differently from
        # every other kind of failure below.
        return None, True
    if p.returncode != 0:
        # A hard CLI failure (crash, auth error, bad flag, ...) that isn't
        # usage-limit-shaped must NOT be treated the same as "the router
        # genuinely found zero matches" -- that would silently misfile
        # files that were never actually routed at all. Mirrors
        # direct_api.stage1_api.run_router()'s philosophy: any
        # failure to get a real answer means "give up on the router for
        # now", signalled the same way as a detected usage limit so the
        # caller falls back to filename-based routing instead of trusting
        # a fabricated empty result.
        logger.warning(f"    router call exited {p.returncode}: {(p.stderr or '').strip()[:300]}")
        return None, True

    # With --output-format json, `claude` prints one JSON object PER LINE
    # describing progress (tool calls, thinking, etc.), and the actual final
    # answer is inside whichever one of those lines has a "result" field.
    # This loop scans every printed line looking for that one, rather than
    # assuming it's always the last line -- but only a line that actually
    # HAS a "result" key updates result_text; other "type"-bearing progress
    # lines (which don't carry that key at all) are skipped rather than
    # blanking out an already-found real answer via `.get("result", "")`'s
    # empty-string default.
    result_text = None
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{") and '"type"' in line:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if "result" in obj:
                result_text = obj["result"]
    if result_text is None:
        # None of the printed lines had the expected wrapper shape --
        # fall back to treating the ENTIRE output as the answer text.
        result_text = p.stdout or ""
    try:
        # result_text should itself be a JSON object matching ROUTER_SCHEMA
        # (that's what --json-schema asked the AI to produce). Parse it.
        return json.loads(result_text), False
    except Exception:
        # The text wasn't valid JSON on its own -- as a last resort, search
        # for the first "{ ... }"-shaped chunk anywhere inside it (the AI
        # may have wrapped its JSON answer in extra prose despite being
        # asked not to) and try parsing just that chunk instead.
        m = re.search(r"\{.*\}", result_text, re.S)
        if m:
            try:
                return json.loads(m.group(0)), False
            except Exception:
                pass
        return None, False


def _parse_matches(obj: dict, buckets: dict):
    """Turn the AI's raw parsed answer (`obj`, matching ROUTER_SCHEMA) into
    a plain list of tuples the rest of the pipeline understands, silently
    dropping any entry that's malformed (missing a required field, wrong
    type) or names a (subject, chapter) combination that isn't one of the
    real, known `buckets` -- an AI answer is never trusted blindly."""
    out = []
    for item in (obj or {}).get("matches", []) or []:
        try:
            full = str(item["subject"]).strip()
            chno = int(item["chapter_no"])
            conf = float(item.get("confidence", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if (full, chno) in buckets:
            role = str(item.get("role") or "").strip() or None
            reason = str(item.get("reason") or "").strip()
            out.append((full, chno, buckets[(full, chno)], role, conf, reason))
    return out


def route_file(path: Path, buckets: dict, prior=None, tracker=None):
    """
    Decide which chapter folder(s) ONE file (`path`) belongs to, using the
    local `claude` command-line program.

    Inputs:
      path    -- the file being routed.
      buckets -- every valid (subject, chapter number) destination this
                 file could be filed under, as a dict mapping
                 (subject, chapter_no) -> folder name.
      prior   -- an optional (subject, chapter_no, folder_name) guess based
                 on the filename alone, mentioned to the AI as a hint it
                 should confirm or override, not blindly trust.
      tracker -- accepted for signature parity with the other two
                 implementations' route_file() functions, but unused here
                 (this implementation doesn't track token counts itself,
                 since the `claude` subprocess call isn't billed per token).

    Returns a (matches, hit_usage_limit, model_used) tuple: `matches` is the
    list produced by _parse_matches() (empty if nothing matched, or if the
    call failed for a reason other than a usage limit).

    In DEV_TOKEN_SAVER_MODE (see src/agents/__init__.py for the shared
    toggle this mirrors): uses a dummy prompt that never asks the CLI to
    actually read `path` (real file reads are this call's token cost
    driver, same reasoning as direct_api.stage1_api.llm_route),
    and skips the escalation call entirely regardless of confidence --
    escalating to a pricier model is the single biggest cost lever here.
    Combined with run_router()'s --max-budget-usd/--effort flags, a full
    real subprocess call (env scrub, JSON parse, the works) costs pennies.
    """
    dev_mode = getattr(config, 'DEV_TOKEN_SAVER_MODE', False)
    tax = "\n".join(f"- {full} | Ch-{chno} | {name}"
                    for (full, chno), name in sorted(buckets.items()))
    if dev_mode:
        prompt = (
            "You are a test agent in DEV_TOKEN_SAVER_MODE. Do NOT read any file. "
            "Return an empty matches array in the required JSON schema, then stop.\n\n"
            f"Valid buckets (for schema reference only):\n{tax}"
        )
    else:
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

    # First attempt: ask the CHEAP, FAST model (Haiku by default) to route
    # the file. Most files are routed correctly on this first try.
    router_model = getattr(config, 'ROUTER_MODEL', 'claude-haiku-4-5-20251001')
    obj, limited = run_router(prompt, router_model)
    if limited:
        return [], True, router_model
    matches = _parse_matches(obj, buckets)
    best = max((m[4] for m in matches), default=0.0)

    # ESCALATION: if the cheap model's best confidence score came back
    # below the configured threshold (CONF_MIN), it isn't sure enough to
    # trust -- so ask a second time, using a more capable (and more
    # expensive) model instead, on the theory that a stronger model is more
    # likely to get an ambiguous case right. This escalation step is
    # skipped entirely in DEV_TOKEN_SAVER_MODE, since it's the single
    # biggest cost driver in this function.
    conf_min = getattr(config, 'CONF_MIN', 0.55)
    if best < conf_min and not dev_mode:
        escalate_model = getattr(config, 'ESCALATE_MODEL', 'claude-sonnet-5')
        obj2, limited2 = run_router(prompt, escalate_model)
        if limited2:
            return [], True, escalate_model
        matches2 = _parse_matches(obj2, buckets)
        # Only actually USE the escalated model's answer if it did at least
        # as well as the cheap model's -- otherwise silently discard it and
        # fall through to returning the original (cheap-model) result below.
        if max((m[4] for m in matches2), default=0.0) >= best:
            return matches2, False, escalate_model

    return matches, False, router_model

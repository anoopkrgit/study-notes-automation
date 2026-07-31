import json
import re
import subprocess
from pathlib import Path

import settings as config
from src.func_tools_and_utils import logger
from .common import build_claude_env, is_usage_limit

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
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(config.LOCAL_RUNTIME_ROOT), env=build_claude_env())
    except Exception as e:
        logger.error(f"    router call failed to launch: {e}")
        return None, False
    combined = (p.stdout or "") + "\n" + (p.stderr or "")
    if is_usage_limit(combined):
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

    result_text = None
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{") and '"type"' in line:
            try:
                result_text = json.loads(line).get("result", "")
            except Exception:
                pass
    if result_text is None:
        result_text = p.stdout or ""
    try:
        return json.loads(result_text), False
    except Exception:
        m = re.search(r"\{.*\}", result_text, re.S)
        if m:
            try:
                return json.loads(m.group(0)), False
            except Exception:
                pass
        return None, False


def _parse_matches(obj: dict, buckets: dict):
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
    Route a file using the local claude CLI.
    tracker is accepted for signature parity but unused.

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

    router_model = getattr(config, 'ROUTER_MODEL', 'claude-haiku-4-5-20251001')
    obj, limited = run_router(prompt, router_model)
    if limited:
        return [], True, router_model
    matches = _parse_matches(obj, buckets)
    best = max((m[4] for m in matches), default=0.0)

    conf_min = getattr(config, 'CONF_MIN', 0.55)
    if best < conf_min and not dev_mode:
        escalate_model = getattr(config, 'ESCALATE_MODEL', 'claude-sonnet-5')
        obj2, limited2 = run_router(prompt, escalate_model)
        if limited2:
            return [], True, escalate_model
        matches2 = _parse_matches(obj2, buckets)
        if max((m[4] for m in matches2), default=0.0) >= best:
            return matches2, False, escalate_model

    return matches, False, router_model

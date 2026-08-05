"""
prompts.py -- builds each agent's "job instructions" (its SYSTEM PROMPT),
and unpacks the "study-notes skill" -- a ZIP file full of rules, example
documents, and helper scripts -- that those instructions are based on.

WHAT IS A "SYSTEM PROMPT", IN PLAIN TERMS
------------------------------------------
Every message sent to Claude can include a special block of instructions,
separate from the back-and-forth conversation, that tells it what role to
play and what rules to follow for the whole conversation -- this is the
"system prompt". Here, Author's system prompt is essentially "you are
writing study notes; here are the detailed formatting/content rules (from
SKILL.md) and a worked example to imitate". Figure's and Compiler's system
prompts are much shorter, since their jobs are narrower (run one script,
report whether it worked).

WHAT IS "templates/study-notes.skill", AND WHY IS IT A ZIP FILE
-------------------------------------------------------------------
It's a packaged bundle (a plain ZIP archive, same format as a .zip you'd
open in a file manager) containing: SKILL.md (the master rulebook for what
a good study-notes document looks like), an `example/` folder (a fully
worked example content.json + figures.json to imitate), a `schema/` folder
(machine-checkable rules for what a valid content.json/figures.json must
contain), and `tools/`+`lib/` (the actual scripts -- ingest, figure-builder,
document-compiler, quality-checkers -- that tools.py in this same folder
wraps and calls). The functions below read files out of that ZIP archive
without needing to permanently unpack it every time (see
ensure_skill_extracted, further down, for the one exception: tool
scripts need to exist as real files on disk to be run as programs, so
that function unpacks the ZIP into a scratch folder once, and reuses it).

DEV_TOKEN_SAVER_MODE IN THIS FILE
------------------------------------
Every *_system_prompt function below starts with a check for
DEV_TOKEN_SAVER_MODE (the cheap smoke-test toggle -- see
src/agents/__init__.py). When it's on, the real rulebook/example content
is skipped entirely and replaced with a single, trivial made-up
instruction, so the agent has nothing substantial to do and finishes in
one or two cheap turns.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import settings as config
from src.common.skill_package import locked_refresh

def load_skill_md_from_zip() -> str:
    """Read SKILL.md (the master content/formatting rulebook) out of the
    templates/study-notes.skill ZIP archive, as plain text. If the archive
    or the file inside it can't be found, fall back to a one-line
    placeholder rather than crashing -- Author would still attempt
    something, just with far less guidance."""
    zip_path = Path('templates/study-notes.skill')
    if not zip_path.exists():
        return "You are an AI assistant helping with study notes."
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            if 'SKILL.md' in zf.namelist():
                return zf.read('SKILL.md').decode('utf-8')
    except Exception:
        pass
    return "You are an AI assistant helping with study notes."

def load_example_json_from_zip(filename: str) -> str:
    """Read one worked-example file (content.json or figures.json) out of
    the skill ZIP's example/ folder, as plain text, so it can be pasted
    straight into Author's system prompt as "here's what a finished one
    looks like". Falls back to an empty JSON object "{}" if missing."""
    zip_path = Path('templates/study-notes.skill')
    if not zip_path.exists():
        return "{}"

    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            target = f"example/{filename}"
            if target in zf.namelist():
                return zf.read(target).decode('utf-8')
    except Exception:
        pass
    return "{}"

def load_schema_from_zip(filename: str) -> str:
    """Read one JSON Schema file (the machine-checkable rules for a valid
    content.json or figures.json) out of the skill ZIP's schema/ folder, as
    plain text. Not currently used to build any prompt directly (the real
    schema CHECKING happens by running tools/validate.py as a subprocess,
    in tools.py) -- kept here as a convenience if a future prompt ever
    wants to show the raw schema text to an agent."""
    zip_path = Path('templates/study-notes.skill')
    if not zip_path.exists():
        return "{}"

    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            target = f"schema/{filename}"
            if target in zf.namelist():
                return zf.read(target).decode('utf-8')
    except Exception:
        pass
    return "{}"

def get_skill_dir() -> Path:
    """Return (creating if needed) the scratch folder where the skill ZIP
    gets unpacked to real files on disk -- see ensure_skill_extracted,
    below, for why that's needed at all."""
    local_runtime = getattr(config, 'LOCAL_RUNTIME_ROOT', Path('.'))
    skill_dir = Path(local_runtime) / '.skill-runtime' / 'study-notes'
    skill_dir.mkdir(parents=True, exist_ok=True)
    return skill_dir

def ensure_skill_extracted() -> Path:
    """Make sure the skill ZIP's tool scripts (ingest.py, figbuild.py,
    build.js, the QA scripts, etc.) exist as real files on disk, and
    return the folder they're in.

    WHY THIS IS NEEDED: reading text out of a ZIP archive (like SKILL.md,
    above) can be done straight from the archive with no extra step. But
    RUNNING a script (e.g. "run figbuild.py on this figures.json") requires
    it to exist as an actual file the operating system can execute -- you
    can't ask the computer to "run the copy of figbuild.py that's still
    zipped up". So this function unpacks the whole archive once into a
    scratch folder (get_skill_dir(), above), and every tool in tools.py
    that needs to run one of these scripts calls this function first to
    get that folder's path.

    This only re-does the unpacking work when the ZIP file itself has
    changed since the last time (compared by file-modification-time, via
    the small ".extracted" marker file) -- so repeated calls during one
    run, or across many runs where the skill hasn't been updated, are
    effectively free.
    """
    skill_dir = get_skill_dir()
    zip_path = Path('templates/study-notes.skill')

    if not zip_path.exists():
        return skill_dir

    zip_mtime = zip_path.stat().st_mtime
    extracted_marker = skill_dir / '.extracted'

    def is_fresh() -> bool:
        return extracted_marker.exists() and extracted_marker.stat().st_mtime >= zip_mtime

    # Cheap lock-free check first -- the common case is "already unpacked,
    # ZIP unchanged", and that shouldn't pay for a lock.
    if is_fresh():
        return skill_dir

    def refresh() -> None:
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(skill_dir)
            extracted_marker.touch()
        except Exception:
            pass

    # The flock + re-check-under-lock dance is shared with
    # stage2_cli.sync_skill_package() -- see src/common/skill_package.py.
    locked_refresh(skill_dir / '.lock', is_fresh, refresh)
    return skill_dir

def author_system_prompt(state: dict) -> list[dict]:
    """Build the Author agent's job instructions: the full SKILL.md
    rulebook plus a worked example of a finished content.json/figures.json,
    told to author its own chapter's version of the same thing.

    Returns a LIST of small text blocks rather than one big string, because
    the Anthropic API lets each block be tagged separately -- here, the big
    rulebook text is tagged "cache_control: ephemeral", meaning Claude's
    provider-side cache can reuse it across repeated calls (e.g. every QA
    retry loop iteration) instead of re-processing the same several-
    hundred-line rulebook from scratch every time, which is both faster
    and cheaper.
    """
    if getattr(config, 'DEV_TOKEN_SAVER_MODE', False):
        # Cheap smoke-test mode: skip the real rulebook/example entirely.
        return [{"type": "text", "text": "You are a test agent in DEV_TOKEN_SAVER_MODE. Output a minimal valid JSON when asked to write content.json or figures.json. Call at most one tool, then stop."}]

    skill_text = load_skill_md_from_zip()
    content_example = load_example_json_from_zip('content.json')
    figures_example = load_example_json_from_zip('figures.json')

    instructions = (
        "Read transcripts, and author content.json and figures.json as appropriate.\n\n"
    )
    
    if getattr(config, 'ENABLE_WEB_ENRICHMENT', False):
        instructions += (
            "You may use tool_web_search and tool_web_fetch to gather additional "
            "context, definitions, or reference material from allowed domains. Use this "
            "sparingly and strictly as directed by SKILL.md. Do not fetch unnecessary pages.\n\n"
        )
        
    instructions += (
        f"Example content.json:\n{content_example}\n\n"
        f"Example figures.json:\n{figures_example}"
    )

    return [
        {"type": "text", "text": skill_text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": instructions}
    ]

def figure_system_prompt(state: dict) -> list[dict]:
    """Build the Figure agent's job instructions -- deliberately tiny,
    since Figure's whole job is: run the figure-drawing script
    (tool_figbuild), look at what it produced, and say whether it worked.
    It never needs the big rulebook Author uses."""
    if getattr(config, 'DEV_TOKEN_SAVER_MODE', False):
        return [{"type": "text", "text": "You are a test agent. Call tool_figbuild once, then stop."}]

    return [{"type": "text", "text": "Run figbuild, view each rendered figure, report success/failure."}]

def compiler_system_prompt(state: dict) -> list[dict]:
    """Build the Compiler agent's job instructions -- also tiny. Compiler's
    job is nearly mechanical: run the document-building script
    (tool_compile_docx); if it fails, explain in plain language what went
    wrong (Compiler is a cheap model used mainly for that error-reading
    step, not for any real creative work)."""
    if getattr(config, 'DEV_TOKEN_SAVER_MODE', False):
        return [{"type": "text", "text": "You are a test agent. Call tool_compile_docx once, then stop."}]

    return [{"type": "text", "text": "Run build.js, if it fails diagnose the error."}]

def triage_system_prompt(state: dict) -> list[dict]:
    """Job instructions for a generic "triage" agent. NOTE: this function is
    not currently wired to anything -- Stage 1's actual triage_node (see
    stage1_graph.py) builds its own routing prompt directly inline instead
    of calling this. Kept here for symmetry with the Stage 2 prompts above,
    in case a future refactor moves Stage 1's prompt-building here too."""
    if getattr(config, 'DEV_TOKEN_SAVER_MODE', False):
        return [{"type": "text", "text": "You are a test agent. Classify the content."}]

    return [{"type": "text", "text": "Triage the content into appropriate buckets."}]

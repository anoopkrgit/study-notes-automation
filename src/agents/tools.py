"""
tools.py -- every concrete ACTION an agent is allowed to ask for: reading a
file, writing content.json, building a figure, compiling the final .docx,
and running the quality checks. This is the "toolbox" referred to in the
module docstring of src/agents/__init__.py.

WHAT A "TOOL" IS, CONCRETELY
------------------------------
Claude cannot run code. What it CAN do is reply "please call the function
named tool_read_source with path=X, chapter_dir=Y". Two things have to
exist for that to work:
  1. A TOOL SCHEMA -- a small JSON description of the tool's name and what
     arguments it expects, so Claude knows the tool exists and how to ask
     for it correctly. See AUTHOR_TOOLS / FIGURE_TOOLS / COMPILER_TOOLS
     near the bottom of this file.
  2. A REAL PYTHON FUNCTION -- the thing that actually runs when that
     request comes back. See TOOL_REGISTRY, also near the bottom, which
     maps each tool's name (a string) to its real function.
base.py's agent loop (make_agent_node) is what actually connects the two:
it sends the schemas to Claude, and looks up the registry when a request
comes back.

MOST TOOLS BELOW ARE THIN WRAPPERS AROUND EXISTING SCRIPTS
---------------------------------------------------------------
Nearly every tool here doesn't do the real work itself in Python -- it
runs one of the scripts that already existed inside the study-notes
"skill" ZIP (ingest.py, figbuild.py, build.js, validate.py, etc. -- see
prompts.py's module docstring for what that ZIP is) as a SEPARATE PROGRAM,
using Python's `subprocess.run(...)`, and reads back whatever that program
printed or wrote to disk. Think of `subprocess.run([...])` as "open a
terminal, type this command, and wait for it to finish" -- it's the same
mechanism a human would use to run these scripts by hand.

DEV_TOKEN_SAVER_MODE: WHICH TOOLS FAKE THEIR WORK, AND WHICH NEVER DO
--------------------------------------------------------------------------
Most tools below check `DEV_TOKEN_SAVER_MODE` (the cheap smoke-test
toggle) and, if it's on, skip the real script entirely and return
made-up/placeholder results instantly. The THREE quality-check tools near
the middle of this file (tool_run_structural_gates, tool_run_quality_gates,
tool_run_document_qa) are the deliberate exception: they ALWAYS run the
real checking scripts, even in dev mode. This is intentional, not an
oversight -- see the comment on tool_run_structural_gates below for why.

WHY SOME TOOLS VALIDATE THEIR PATHS (_validate_path, right below)
----------------------------------------------------------------------
Claude decides what arguments to pass a tool, including file paths. A
tool that just opened whatever path it was given, with no check, could in
principle be tricked (by a confused or manipulated model) into reading or
writing a file far outside the one chapter folder it's supposed to be
working in. _validate_path stops that: it refuses any path that doesn't
resolve to somewhere inside the chapter's own folder.
"""

from __future__ import annotations

import os
import json
import tempfile
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Union

import settings as config
import urllib.request
import urllib.parse
from html.parser import HTMLParser

try:
    from ddgs import DDGS
except ImportError:
    # We will assume it's installed or provided, but fallback gracefully if missing
    DDGS = None

# A very basic HTML to text parser for tool_web_fetch
class SimpleHTMLToText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.in_body = False
        self.ignore_tags = {'script', 'style', 'head', 'meta', 'link'}
        self.current_ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'body':
            self.in_body = True
        if tag in self.ignore_tags:
            self.current_ignore += 1
        if tag in {'p', 'br', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li'}:
            self.text_parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.ignore_tags and self.current_ignore > 0:
            self.current_ignore -= 1
        if tag == 'body':
            self.in_body = False

    def handle_data(self, data):
        if self.in_body and self.current_ignore == 0:
            cleaned = data.strip()
            if cleaned:
                self.text_parts.append(cleaned + " ")

    def get_text(self):
        return "".join(self.text_parts).strip()

from src.func_tools_and_utils import (
    logger,
    tool_view_pdf_page,
    tool_view_image,
    tool_convert_to_png
)
from src.agents.prompts import ensure_skill_extracted


def _validate_path(requested: str, allowed_root: str) -> Path:
    """Safety check used by every tool that takes a file path from Claude:
    make sure `requested` is actually somewhere inside `allowed_root` (the
    current chapter's own folder) before it's used, and refuse it
    otherwise. In plain terms: this stops a tool call like
    "read ../../../etc/passwd" from ever reaching the real filesystem call
    that would open it -- every path is first re-anchored to, and checked
    against, the one folder this tool is allowed to touch.
    Raises ValueError if the path would escape allowed_root."""
    requested_path = Path(requested).resolve()
    root_path = Path(allowed_root).resolve()
    try:
        requested_path.relative_to(root_path)
    except ValueError:
        raise ValueError(f"Path traversal attempt: {requested} is not within {allowed_root}")
    return requested_path


# -----------------------------------------------------------------------------
# 1. Ingest tools (used by ingest_node in stage2_graph.py)
#
# "Deterministic" means: no Claude call happens here at all. This runs
# automatically, the same way every time for the same input, before Author
# even starts -- it's plain Python calling a script, not an agent deciding
# what to do.
# -----------------------------------------------------------------------------
def tool_ingest(pdf_path: str, chapter_dir: str) -> dict:
    """Take one source PDF (a class transcript or textbook scan) and turn it
    into whatever Author can actually read: either plain extracted text, or
    -- if the PDF is mostly images/handwriting -- auto-cropped page images.
    Runs tools/ingest.py as a separate program (see the module docstring's
    "MOST TOOLS BELOW ARE THIN WRAPPERS" section) and reads back what it
    produced. Results are cached by the file's SHA256 checksum (a short
    fingerprint of the file's exact contents), so re-running on the same
    PDF a second time skips the work instead of redoing it.

    Inputs: pdf_path (the source PDF to read), chapter_dir (this chapter's
    own folder, where extracted files get written).
    Output: {ok: bool, extracted_files: list[str], errors: list[str]}.
    In DEV_TOKEN_SAVER_MODE: skip the subprocess entirely, return dummy data.
    """
    if getattr(config, "DEV_TOKEN_SAVER_MODE", False):
        return {"ok": True, "extracted_files": ["dummy.txt"], "errors": []}

    skill_dir = Path(ensure_skill_extracted())
    ingest_script = skill_dir / 'tools' / 'ingest.py'
    
    result = subprocess.run(
        ['python3', str(ingest_script), pdf_path, '--out', chapter_dir],
        capture_output=True, text=True
    )
    
    if result.returncode == 0:
        extracted = [str(p) for p in Path(chapter_dir).rglob('*') if p.is_file()]
        return {"ok": True, "extracted_files": extracted, "errors": []}
    else:
        return {"ok": False, "extracted_files": [], "errors": [result.stderr]}


# -----------------------------------------------------------------------------
# 2. Author tools -- the only tools that let the AGENT itself read source
# material and write out the chapter's content/figures. See AUTHOR_TOOLS
# further down for the matching tool-schema list handed to Claude.
# -----------------------------------------------------------------------------
def tool_read_source(path: str, chapter_dir: str) -> str:
    """Let Author read the plain-text content of one source file (a
    transcript or supporting-material text file already produced by
    tool_ingest, above). `path` is checked with _validate_path first, so
    Author can only read files inside its own chapter's folder.

    Inputs: path (the file to read), chapter_dir (the folder it must be
    inside). Output: the file's text content as a plain string (or an
    "Error: ..." string if the file is missing/unreadable -- returned as
    text rather than raised as an exception, since that's how the agent
    loop feeds tool results back to Claude either way).
    In DEV_TOKEN_SAVER_MODE: skip the real file, return placeholder text.
    """
    if getattr(config, "DEV_TOKEN_SAVER_MODE", False):
        return "Sample transcript content for testing."
        
    safe_path = _validate_path(path, chapter_dir)
    if not safe_path.is_file():
        return f"Error: File not found at {path}"
        
    try:
        return safe_path.read_text(encoding='utf-8')
    except Exception as e:
        return f"Error reading file: {str(e)}"

def tool_view_source_page(path: str, page: int, chapter_dir: str) -> list:
    """Let Author actually SEE one page of a source PDF as an image, for
    cases where the raw extracted text (tool_read_source) isn't enough --
    e.g. a diagram, a handwritten equation, or a table whose layout matters.
    `path` is validated the same way as tool_read_source. The real
    PDF-to-image conversion is shared plumbing (tool_view_pdf_page, in
    func_tools_and_utils.py) reused by several tools, not reimplemented here.
    Output: a list of content blocks in the format the Anthropic API expects
    for "here is an image" (not a plain string, unlike tool_read_source)."""
    safe_path = _validate_path(path, chapter_dir)
    return tool_view_pdf_page(str(safe_path), page)

def tool_write_content_json(content: dict, chapter_dir: str) -> dict:
    """Save Author's finished chapter TEXT as content.json -- the file that
    holds every section, explanation, and worked example Author has
    written, in a structured (not free-form) shape the rest of the
    pipeline can read reliably. Before writing, the content is checked
    against schema/content.schema.json (a machine-readable list of "a
    valid content.json must have these fields, in this shape") by running
    tools/validate.py as a separate program -- if it fails that check, the
    file is NOT written, and the errors are handed back so Author can see
    what was wrong and try again.

    Inputs: content (the dict Author wants to save), chapter_dir (where to
    save it). Output: {ok: bool, errors: list[str]}.
    In DEV_TOKEN_SAVER_MODE: skip validation, write whatever was given.
    """
    if getattr(config, "DEV_TOKEN_SAVER_MODE", False):
        out_path = Path(chapter_dir) / "content.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(content, indent=2), encoding='utf-8')
        return {"ok": True, "errors": []}

    skill_dir = Path(ensure_skill_extracted())
    validate_script = skill_dir / 'tools' / 'validate.py'
    schema_path = skill_dir / 'schema' / 'content.schema.json'
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp:
        json.dump(content, tmp)
        tmp_path = tmp.name
        
    try:
        result = subprocess.run(
            ['python3', str(validate_script), tmp_path, '--schema', str(schema_path)],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            out_path = Path(chapter_dir) / "content.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(content, indent=2), encoding='utf-8')
            return {"ok": True, "errors": []}
        else:
            return {"ok": False, "errors": [result.stderr or result.stdout]}
    finally:
        os.remove(tmp_path)

def tool_write_figures_json(figures: dict, chapter_dir: str) -> dict:
    """Same idea as tool_write_content_json just above, but for DIAGRAM
    SPECIFICATIONS instead of text: figures.json describes what each
    diagram in the chapter should look like (labels, shapes, layout) as
    structured data -- not the image itself, just the instructions for
    drawing it. tool_figbuild (further below) is what actually turns this
    into a real picture. Validated against schema/figures.schema.json the
    same way, before being written to <chapter_dir>/figures.json.

    Output: {ok: bool, errors: list[str]}.
    In DEV_TOKEN_SAVER_MODE: skip validation, write whatever was given.
    """
    if getattr(config, "DEV_TOKEN_SAVER_MODE", False):
        out_path = Path(chapter_dir) / "figures.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(figures, indent=2), encoding='utf-8')
        return {"ok": True, "errors": []}

    skill_dir = Path(ensure_skill_extracted())
    validate_script = skill_dir / 'tools' / 'validate.py'
    schema_path = skill_dir / 'schema' / 'figures.schema.json'
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp:
        json.dump(figures, tmp)
        tmp_path = tmp.name
        
    try:
        result = subprocess.run(
            ['python3', str(validate_script), tmp_path, '--schema', str(schema_path)],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            out_path = Path(chapter_dir) / "figures.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(figures, indent=2), encoding='utf-8')
            return {"ok": True, "errors": []}
        else:
            return {"ok": False, "errors": [result.stderr or result.stdout]}
    finally:
        os.remove(tmp_path)

def tool_read_qa_feedback(chapter_dir: str) -> dict:
    """When Author is being asked to try again after a failed quality check
    (see the QA tools further below, and qa_node in stage2_graph.py), this
    lets Author read exactly what went wrong last time, so the retry can
    fix the actual reported problems instead of guessing.
    Output: the saved qa_report dict, or {} if there isn't one yet (i.e.
    this is Author's first attempt, not a retry)."""
    if getattr(config, "DEV_TOKEN_SAVER_MODE", False):
        return {}
    
    qa_path = Path(chapter_dir) / "qa_report.json"
    if qa_path.exists():
        try:
            return json.loads(qa_path.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def tool_web_search(query: str, allowed_domains: list[str], chapter_dir: str) -> str:
    """
    Search the web for a query, constrained to allowed domains.
    Returns a list of URLs and snippets.
    """
    if getattr(config, 'DEV_TOKEN_SAVER_MODE', False):
        logger.info(f"[tool_web_search] DEV MODE mock search for: {query}")
        return json.dumps([{"url": "https://en.wikipedia.org/wiki/Mock", "title": "Mock", "snippet": "Mock search result."}])

    if not DDGS:
        return json.dumps({"error": "ddgs library not installed. Web search unavailable."})

    # Only 5 allowed domains in the system
    global_allowed = {"en.wikipedia.org", "simple.wikipedia.org", "khanacademy.org", "byjus.com", "britannica.com"}
    
    # Enforce allowed domains
    validated_domains = []
    for d in allowed_domains:
        if any(d == gd or d.endswith("." + gd) for gd in global_allowed):
            validated_domains.append(d)
    
    if not validated_domains:
        return json.dumps({"error": f"None of the requested domains are in the global whitelist: {global_allowed}"})
        
    site_query = " OR ".join([f"site:{d}" for d in validated_domains])
    full_query = f"{query} ({site_query})"
    
    logger.info(f"[tool_web_search] Searching: {full_query}")
    try:
        results = DDGS().text(full_query, max_results=5)
        # DuckDuckGo sometimes returns empty lists if no results
        if not results:
            return json.dumps([])
        return json.dumps([{"url": r.get('href'), "title": r.get('title'), "snippet": r.get('body')} for r in results])
    except Exception as e:
        logger.error(f"[tool_web_search] Error: {e}")
        return json.dumps({"error": f"Search failed: {str(e)}"})

def tool_web_fetch(url: str, chapter_dir: str) -> str:
    """
    Fetch and return the readable text content of a URL.
    """
    if getattr(config, 'DEV_TOKEN_SAVER_MODE', False):
        logger.info(f"[tool_web_fetch] DEV MODE mock fetch for: {url}")
        return "Mock content for web fetch."

    # Validate domain
    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc
    global_allowed = {"en.wikipedia.org", "simple.wikipedia.org", "khanacademy.org", "byjus.com", "britannica.com"}
    
    if not any(domain == gd or domain.endswith("." + gd) for gd in global_allowed):
        return json.dumps({"error": f"Domain {domain} is not in the global whitelist: {global_allowed}"})

    logger.info(f"[tool_web_fetch] Fetching: {url}")
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8', errors='ignore')
            parser = SimpleHTMLToText()
            parser.feed(html)
            text = parser.get_text()
            
            # Cap the length to avoid blowing up the context window
            max_len = 15000
            if len(text) > max_len:
                text = text[:max_len] + "\n\n...[CONTENT TRUNCATED]..."
            return text
    except Exception as e:
        logger.error(f"[tool_web_fetch] Error fetching {url}: {e}")
        return json.dumps({"error": f"Fetch failed: {str(e)}"})


# -----------------------------------------------------------------------------
# 3. Figure tools -- used by the Figure agent to turn figures.json (a
# written-out description of a diagram) into an actual PNG image, and then
# look at that image to confirm it rendered correctly.
# -----------------------------------------------------------------------------
def tool_figbuild(figures_json_path: str, chapter_dir: str) -> dict:
    """Turn the diagram SPECIFICATIONS in figures.json into actual PICTURE
    files (PNG images) that can be embedded in the finished document. Runs
    lib/figbuild.py as a separate program to do the actual drawing.
    Output: {ok: bool, rendered: list[str] (paths to the PNGs it made),
    errors: list[str]}.
    In DEV_TOKEN_SAVER_MODE: skip drawing, return a fake file name.
    """
    if getattr(config, "DEV_TOKEN_SAVER_MODE", False):
        return {"ok": True, "rendered": ["dummy_fig.png"], "errors": []}

    skill_dir = Path(ensure_skill_extracted())
    figbuild_script = skill_dir / 'lib' / 'figbuild.py'
    
    result = subprocess.run(
        ['python3', str(figbuild_script), figures_json_path, '--out', chapter_dir],
        capture_output=True, text=True
    )
    
    if result.returncode == 0:
        return {"ok": True, "rendered": [result.stdout.strip()], "errors": []}
    else:
        return {"ok": False, "rendered": [], "errors": [result.stderr or result.stdout]}

def tool_view_figure(png_path: str, chapter_dir: str) -> list:
    """Let the Figure agent actually LOOK at a diagram tool_figbuild just
    drew, so it can judge (as Claude, looking at the image) whether the
    picture came out right before reporting success. Same image-block
    output format as tool_view_source_page, above."""
    safe_path = _validate_path(png_path, chapter_dir)
    return tool_view_image(str(safe_path))


# -----------------------------------------------------------------------------
# 4. Compiler tools -- used by the Compiler agent to turn the finished
# content.json + rendered figures into the actual Word document (.docx)
# the whole pipeline exists to produce.
# -----------------------------------------------------------------------------
def tool_compile_docx(content_json_path: str, figures_json_path: str, chapter_dir: str) -> dict:
    """The final assembly step: combine the written text (content.json) and
    the rendered diagram images (from tool_figbuild) into one actual Word
    document (.docx) -- this is the file the whole pipeline exists to
    produce. Runs lib/build.js (a Node.js program, not Python -- hence
    `node` instead of `python3` in the subprocess call below) to do the
    real document assembly/formatting work.
    Output: {ok: bool, docx_path: str | None, stderr: str | None}.
    In DEV_TOKEN_SAVER_MODE: skip real assembly, write a placeholder file.
    """
    docx_path = str(Path(chapter_dir) / "output.docx")
    
    if getattr(config, "DEV_TOKEN_SAVER_MODE", False):
        Path(docx_path).write_text("Dummy docx content", encoding='utf-8')
        return {"ok": True, "docx_path": docx_path, "stderr": None}

    skill_dir = Path(ensure_skill_extracted())
    build_script = skill_dir / 'lib' / 'build.js'
    
    # Deriving figures directory assuming figures are written inside chapter_dir
    figs_dir = chapter_dir
    
    result = subprocess.run(
        ['node', str(build_script), content_json_path, '--figs', figs_dir, '-o', docx_path],
        capture_output=True, text=True
    )
    
    if result.returncode == 0:
        return {"ok": True, "docx_path": docx_path, "stderr": None}
    else:
        return {"ok": False, "docx_path": None, "stderr": result.stderr or result.stdout}


# -----------------------------------------------------------------------------
# 5. QA tools -- called DIRECTLY, as plain Python function calls, by
# qa_node in stage2_graph.py. Unlike every tool above, these are NOT
# offered to any Claude agent to request -- there is no "QA agent" making
# a judgement call here. Every one of these checks is a deterministic
# pass/fail script (does the JSON match the required structure? does every
# answer's arithmetic actually check out? does the Word document open
# correctly?), so there's nothing for an AI model to decide -- qa_node
# just runs all of them and adds up the results. This is a deliberate
# design choice: it keeps the one part of the pipeline that decides
# "is this chapter actually finished?" fully predictable and free of any
# model cost.
# -----------------------------------------------------------------------------
def tool_run_structural_gates(content_json_path: str, figures_json_path: str) -> dict:
    """The first of three automated "quality gates" (checkpoints a chapter
    must pass before it's considered finished -- see the module-level
    comment above this section for why these are plain deterministic
    checks, not an AI judgment call). This one checks the STRUCTURE of the
    written files, in three steps:
      1. schema check -- does content.json/figures.json have all the
         required fields, in the right shape? ("schema" here just means
         "the rulebook describing what fields a valid file must contain".)
      2. verify.py -- deeper content checks beyond just having the right
         fields (e.g. does the arithmetic in worked examples actually add
         up).
      3. invariants.py -- checks rules that must ALWAYS hold no matter what
         (e.g. every figure mentioned in the text actually exists in
         figures.json).
    Always runs for real, even in DEV_TOKEN_SAVER_MODE -- QA gates are the one
    thing that dev mode must NOT fake, since they're what proves the QA<->Author
    retry loop actually works. Dev-mode dummy content is expected to genuinely
    fail these checks; that's the point of the smoke test, not a bug."""
    skill_dir = Path(ensure_skill_extracted())
    failures = []
    schema_ok = verify_ok = invariants_ok = True

    # schema (content & figures)
    validate_script = skill_dir / 'tools' / 'validate.py'
    schema_content = skill_dir / 'schema' / 'content.schema.json'
    schema_figs = skill_dir / 'schema' / 'figures.schema.json'
    
    r1 = subprocess.run(['python3', str(validate_script), content_json_path, '--schema', str(schema_content)], capture_output=True, text=True)
    if r1.returncode != 0:
        schema_ok = False
        failures.append(f"Content Schema: {r1.stderr}")
        
    r2 = subprocess.run(['python3', str(validate_script), figures_json_path, '--schema', str(schema_figs)], capture_output=True, text=True)
    if r2.returncode != 0:
        schema_ok = False
        failures.append(f"Figures Schema: {r2.stderr}")

    # verify.py
    verify_script = skill_dir / 'tools' / 'verify.py'
    r3 = subprocess.run(['python3', str(verify_script), content_json_path], capture_output=True, text=True)
    if r3.returncode != 0:
        verify_ok = False
        failures.append(f"Verify: {r3.stderr}")

    # invariants.py
    invariants_script = skill_dir / 'tools' / 'invariants.py'
    r4 = subprocess.run(['python3', str(invariants_script), content_json_path], capture_output=True, text=True)
    if r4.returncode != 0:
        invariants_ok = False
        failures.append(f"Invariants: {r4.stderr}")
        
    return {
        "schema_ok": schema_ok,
        "verify_ok": verify_ok,
        "invariants_ok": invariants_ok,
        "failures": failures
    }

def tool_run_quality_gates(content_json_path: str, chapter_dir: str) -> dict:
    """The second quality gate: checks whether the chapter is actually GOOD
    teaching material, not just structurally valid. pedagogy.py checks
    whether the explanations follow the project's teaching style rules
    (e.g. worked examples before practice problems); baseline.py compares
    this chapter's document against a known-good reference chapter to
    catch quality regressions.
    Always runs for real, even in DEV_TOKEN_SAVER_MODE -- see tool_run_structural_gates
    docstring for why QA gates specifically are never stubbed."""
    skill_dir = Path(ensure_skill_extracted())
    pedagogy_script = skill_dir / 'tools' / 'pedagogy.py'
    baseline_script = skill_dir / 'tools' / 'baseline.py'
    docx_path = str(Path(chapter_dir) / 'output.docx')

    pedagogy_ok = baseline_ok = True
    deltas = {}

    r1 = subprocess.run(['python3', str(pedagogy_script), content_json_path], capture_output=True, text=True)
    if r1.returncode != 0:
        pedagogy_ok = False
        deltas["pedagogy"] = r1.stderr

    # baseline.py's real signature is: baseline.py <docx> --content <content.json> [...]
    # (positional docx path, required --content flag) -- there is no --dir flag.
    r2 = subprocess.run(['python3', str(baseline_script), docx_path, '--content', content_json_path], capture_output=True, text=True)
    if r2.returncode != 0:
        baseline_ok = False
        deltas["baseline"] = r2.stderr
        
    return {
        "pedagogy_ok": pedagogy_ok,
        "baseline_ok": baseline_ok,
        "deltas": deltas
    }

def tool_run_document_qa(docx_path: str, content_json_path: str, do_pdf_check: bool = False) -> dict:
    """The third quality gate: checks the actual FINISHED WORD DOCUMENT
    (not the underlying JSON) -- does the .docx file open without
    corruption ("OOXML" is the technical file format Word documents are
    built from), does it contain the math it's supposed to, are page
    breaks placed safely, and does every figure referenced in the text
    correspond to a real image in the document. `do_pdf_check=True` adds a
    slower extra check that also renders the document to PDF and inspects
    it page by page.
    Always runs for real, even in DEV_TOKEN_SAVER_MODE -- see tool_run_structural_gates
    docstring for why QA gates specifically are never stubbed."""
    skill_dir = Path(ensure_skill_extracted())
    qa_script = skill_dir / 'tools' / 'qa.py'

    cmd = ['python3', str(qa_script), docx_path, '--content', content_json_path]
    if do_pdf_check:
        cmd.append('--pdf')  # qa.py's real flag is --pdf, not --pdf-check
        
    r = subprocess.run(cmd, capture_output=True, text=True)
    return {
        "ok": r.returncode == 0,
        "issues": [r.stderr] if r.returncode != 0 else [],
        "pdf_checked": do_pdf_check
    }


# -----------------------------------------------------------------------------
# Anthropic Tool Schemas
#
# These lists are what actually gets sent to Claude (see the
# `tools=tools_for_anthropic` argument in base.py's make_agent_node) -- they
# describe each tool's NAME and the shape of arguments it expects, in a
# format Claude understands, so it can correctly request "please call
# tool_read_source with path=... and chapter_dir=...". Note these are pure
# descriptions/JSON, not the functions themselves -- the actual Python code
# that runs when a tool is requested lives in TOOL_REGISTRY, further below.
# Author, Figure, and Compiler each get their OWN separate list here, so
# (for example) Figure is never even offered the option of calling
# tool_compile_docx -- keeping each agent's available actions limited to
# its own narrow job, unlike the old design's single flat list of every
# tool for every purpose.
# -----------------------------------------------------------------------------

AUTHOR_TOOLS = [
    {
        "name": "tool_read_source",
        "description": "Read one transcript or supporting text file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the source text file"},
                "chapter_dir": {"type": "string", "description": "The current chapter directory"}
            },
            "required": ["path", "chapter_dir"]
        }
    },
    {
        "name": "tool_view_source_page",
        "description": "Render one PDF page of a source doc as an image.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the PDF file"},
                "page": {"type": "integer", "description": "The page number to view (1-indexed)"},
                "chapter_dir": {"type": "string", "description": "The current chapter directory"}
            },
            "required": ["path", "page", "chapter_dir"]
        }
    },
    {
        "name": "tool_write_content_json",
        "description": "Write chapter content to content.json after validating against schema.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "object", "description": "The JSON object containing the chapter content"},
                "chapter_dir": {"type": "string", "description": "The current chapter directory"}
            },
            "required": ["content", "chapter_dir"]
        }
    },
    {
        "name": "tool_write_figures_json",
        "description": "Write chapter figures to figures.json after validating against schema.",
        "input_schema": {
            "type": "object",
            "properties": {
                "figures": {"type": "object", "description": "The JSON object containing the chapter figures"},
                "chapter_dir": {"type": "string", "description": "The current chapter directory"}
            },
            "required": ["figures", "chapter_dir"]
        }
    },
    {
        "name": "tool_read_qa_feedback",
        "description": "Read the QA report for feedback.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chapter_dir": {"type": "string", "description": "The current chapter directory"}
            },
            "required": ["chapter_dir"]
        }
    }
]

FIGURE_TOOLS = [
    {
        "name": "tool_figbuild",
        "description": "Build rendered PNGs from the figures.json file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "figures_json_path": {"type": "string", "description": "Path to figures.json"},
                "chapter_dir": {"type": "string", "description": "Output directory for the generated figure images"}
            },
            "required": ["figures_json_path", "chapter_dir"]
        }
    },
    {
        "name": "tool_view_figure",
        "description": "View a rendered figure PNG.",
        "input_schema": {
            "type": "object",
            "properties": {
                "png_path": {"type": "string", "description": "Path to the PNG figure"},
                "chapter_dir": {"type": "string", "description": "The current chapter directory (for safety scoping)"}
            },
            "required": ["png_path", "chapter_dir"]
        }
    }
]

COMPILER_TOOLS = [
    {
        "name": "tool_compile_docx",
        "description": "Compile the final DOCX file from the content.json and rendered figures.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content_json_path": {"type": "string", "description": "Path to content.json"},
                "figures_json_path": {"type": "string", "description": "Path to figures.json"},
                "chapter_dir": {"type": "string", "description": "Output directory for the compiled docx file"}
            },
            "required": ["content_json_path", "figures_json_path", "chapter_dir"]
        }
    }
]

WEB_ENRICHMENT_TOOLS = [
    {
        "name": "tool_web_search",
        "description": "Search the web for educational material. You MUST pass at least one allowed domain.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "allowed_domains": {
                    "type": "array", 
                    "items": {"type": "string"}, 
                    "description": "List of domains to restrict the search to. Allowed: en.wikipedia.org, simple.wikipedia.org, khanacademy.org, byjus.com, britannica.com"
                },
                "chapter_dir": {"type": "string", "description": "The current chapter directory"}
            },
            "required": ["query", "allowed_domains", "chapter_dir"]
        }
    },
    {
        "name": "tool_web_fetch",
        "description": "Fetch the readable text content of a specific URL returned by tool_web_search.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch (must be from the allowed domains)"},
                "chapter_dir": {"type": "string", "description": "The current chapter directory"}
            },
            "required": ["url", "chapter_dir"]
        }
    }
]


# -----------------------------------------------------------------------------
# Tool Registry
#
# The other half of the picture from the schemas above: a lookup table from
# tool NAME (the same string Claude uses when it asks for a tool) to the
# REAL Python function that runs when that request comes in. When Claude
# replies "please call tool_figbuild with these arguments", base.py's agent
# loop does `tool_registry['tool_figbuild'](**those_arguments)` -- i.e.
# looks the name up here and actually calls the matching function above.
# Note tool_ingest and tool_run_*_gates are NOT in this registry: they are
# called directly by plain Python code (ingest_node and qa_node in
# stage2_graph.py), never requested by an agent, so they don't need a
# name-to-function entry here.
# -----------------------------------------------------------------------------
TOOL_REGISTRY = {
    'tool_read_source': tool_read_source,
    'tool_view_source_page': tool_view_source_page,
    'tool_write_content_json': tool_write_content_json,
    'tool_write_figures_json': tool_write_figures_json,
    'tool_read_qa_feedback': tool_read_qa_feedback,
    'tool_figbuild': tool_figbuild,
    'tool_view_figure': tool_view_figure,
    'tool_compile_docx': tool_compile_docx,
    'tool_web_search': tool_web_search,
    'tool_web_fetch': tool_web_fetch,
}

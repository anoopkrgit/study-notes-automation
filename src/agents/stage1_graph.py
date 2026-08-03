"""
stage1_graph.py -- Stage 1 (Triage/Extraction): decides which chapter a
newly-arrived file (a class transcript PDF, or a piece of extra reference
material) actually belongs to.

WHEN THIS RUNS
----------------
Once per incoming file, NOT once per chapter -- this is a much smaller job
than Stage 2 (see stage2_graph.py), closer to a quick filing decision than
a creative writing task. It only runs when Stage 1 is set to the "graph"
implementation (STUDY_NOTES_STAGE1_IMPL=graph); see dispatch.py for how
that choice is made, and note that as of this writing the graph path built
here is not yet wired into the real file-processing loop (see
stage1_api.py) -- the "legacy" single-call router remains the
one actually used by default.

THE THREE-STEP FLOWCHART (a "graph" of three nodes)
--------------------------------------------------------
    extract_node  -->  triage_node  -->  reconcile_node
1. extract_node   -- plain Python, no AI call. Pulls a short excerpt out of
                     the file (first few PDF pages, or first few thousand
                     characters of text) -- just enough for a model to
                     recognise the subject and chapter from, without
                     needing to read the whole document.
2. triage_node    -- the one step that actually calls Claude. Shown that
                     excerpt plus the list of valid "subject + chapter"
                     bins, it's asked to pick which bin(s) this file
                     belongs to, using a single, forced tool call
                     (ROUTER_TOOL, below) so the answer always comes back
                     in the same predictable shape.
3. reconcile_node -- plain Python again. Cleans up Claude's raw answer:
                     drops any malformed entries, and passes through
                     unchanged if the API was unavailable for this file
                     (so the caller knows to fall back to filing by
                     filename alone).
Only extract_node and triage_node do meaningful work per file; reconcile_
node exists as its own separate step mainly so the "confident disagreement
-> flag for human review" logic (were it added here) would have an
obvious, single place to live, matching the shape of Stage 2's flowchart.

route_one_file(), at the bottom of this file, is the single function
outside code calls to run this whole three-step flowchart for one file
and get back a plain answer -- see its own docstring, below.
"""

from __future__ import annotations

import io
import base64
from pathlib import Path
from typing import Literal

from langgraph.graph import StateGraph, END
import anthropic

import settings as config
from src.func_tools_and_utils import logger, TokenTracker, classify_api_error, is_ignorable
from src.agents.base import Stage1State

# The tool schema Claude is FORCED to answer with (see triage_node's
# tool_choice={"type": "tool", "name": "route_file"} below) -- forcing a
# specific tool, instead of letting the model choose freely, guarantees
# the reply always comes back as this exact structured shape (a list of
# subject/chapter/confidence entries) rather than free-form prose that
# would need to be parsed and could vary in format from call to call.
ROUTER_TOOL = {
    "name": "route_file",
    "description": "Return chapter routing decision for this file.",
    "input_schema": {
        "type": "object", "additionalProperties": False, "required": ["matches"],
        "properties": {
            "matches": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["subject", "chapter_no", "confidence"],
                "properties": {
                    "subject": {"type": "string", "enum": ["Physics", "Chemistry", "Maths"]},
                    "chapter_no": {"type": "integer"},
                    # "spine" = a class transcript (the file this chapter's
                    # notes are mainly built from); "supporting" = extra
                    # reference material for the same chapter (see
                    # Stage2State's transcripts/supporting fields in base.py).
                    "role": {"type": "string", "enum": ["spine", "supporting"]},
                    # A 0-1 number: how sure Claude is about this match. Not
                    # currently used to auto-accept/reject anything here --
                    # kept for logging and possible future "flag low-
                    # confidence matches for human review" logic.
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},  # Claude's own one-line explanation, for logs
                }}},
            "agrees_with_filename": {"type": ["boolean", "null"]},
        }
    }
}

def extract_node(state: Stage1State) -> dict:
    """Step 1 of 3. Plain Python, no AI call: pull a short, cheap-to-send
    excerpt out of the file being classified, in whatever form is easiest
    for Claude to actually read:
      - PDF   -> keep only the first few pages (config.SNIPPET_PAGES),
                 and send them as an embedded PDF document (Claude can
                 read PDFs directly, including scanned/image-only pages).
      - .docx -> extract its plain text, trimmed to a max character count
                 (config.SNIPPET_CHARS).
      - anything else -> read it as plain text, same trimming.
    Returns {'extracted_content': ...} -- the one field this step changes
    in the shared Stage1State clipboard; None if extraction failed for any
    reason (missing library, unreadable file, etc.), which triage_node
    (below) treats as "nothing to classify"."""
    file_path = state['file_path']
    extracted_content = None
    try:
        path = Path(file_path)
        if path.suffix.lower() == '.pdf':
            try:
                # pypdf is a Python library for reading/writing PDF files.
                # PdfReader opens the original file; PdfWriter is used here
                # to build a brand-new, SMALLER PDF containing only the
                # first few pages, so only that trimmed excerpt (not the
                # whole document) gets sent to Claude.
                from pypdf import PdfReader, PdfWriter
                reader = PdfReader(path)
                pages_to_extract = min(getattr(config, 'SNIPPET_PAGES', 5), len(reader.pages))

                writer = PdfWriter()
                for i in range(pages_to_extract):
                    writer.add_page(reader.pages[i])

                # Claude's API is a web/JSON API, which can only carry text,
                # not raw binary files. "base64" is a standard way to turn
                # arbitrary binary data (like PDF bytes) into plain text
                # characters so it can be embedded inside a JSON message and
                # safely travel over the network; the API decodes it back
                # into the real PDF on Anthropic's end.
                out_stream = io.BytesIO()
                writer.write(out_stream)
                pdf_bytes = out_stream.getvalue()
                encoded_pdf = base64.b64encode(pdf_bytes).decode('utf-8')

                extracted_content = [{
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": encoded_pdf
                    }
                }]
            except ImportError:
                logger.warning("pypdf not installed, cannot extract PDF")
        elif path.suffix.lower() == '.docx':
            try:
                from docx import Document
                doc = Document(path)
                text_content = "\n".join(p.text for p in doc.paragraphs)
                extracted_content = [{"type": "text", "text": text_content[:getattr(config, 'SNIPPET_CHARS', 5000)]}]
            except ImportError:
                logger.warning("python-docx not installed, cannot extract docx")
        else:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                text_content = f.read(getattr(config, 'SNIPPET_CHARS', 5000))
            extracted_content = [{"type": "text", "text": text_content}]
    except Exception as e:
        logger.error(f"Error extracting content from {file_path}: {e}")
    
    return {'extracted_content': extracted_content}

def triage_node(state: Stage1State) -> dict:
    """Step 2 of 3. The one step in this flowchart that actually calls
    Claude. Sends it the excerpt from extract_node plus the list of valid
    "subject + chapter" bins (state['buckets']), and asks it to decide
    which bin(s) this file belongs to -- using the FORCED ROUTER_TOOL
    (above) so the answer always comes back in the same shape, never as
    free-form prose.

    This is a single, one-shot API call -- unlike Stage 2's agents (see
    base.py), there is no multi-turn back-and-forth loop here; the model
    is asked once and must answer once, since "classify this one file" is
    a much simpler decision than "author a whole chapter"."""
    extracted_content = state.get('extracted_content')
    if not extracted_content:
        return {'matches': [], 'limited': False, 'model_used': ''}

    model = getattr(config, 'TRIAGE_MODEL', getattr(config, 'ROUTER_MODEL', 'claude-haiku-4-5'))
    is_dev = getattr(config, 'DEV_TOKEN_SAVER_MODE', False)
    if is_dev:
        # DEV_TOKEN_SAVER_MODE forces every agent to the same cheap model used
        # elsewhere in the project (config.FIGURE_MODEL == claude-haiku-4-5).
        model = getattr(config, 'FIGURE_MODEL', 'claude-haiku-4-5')

    # `anthropic.Anthropic()` opens a connection to Claude's API (reading
    # the API key from the environment). `tracker`, if the caller supplied
    # one via route_one_file (below), gets this call's usage recorded onto
    # it for logging/cost tracking -- it has no effect on the
    # classification itself. If no tracker was supplied, usage for this
    # call simply isn't recorded anywhere (matching llm_route()'s own
    # `if tracker is not None` pattern in direct_api/stage1_api.py), rather
    # than silently creating and discarding a throwaway local one.
    client = anthropic.Anthropic()
    tracker = state.get('tracker')

    prompt = f"File: {state['file_path']}\nAvailable buckets: {state['buckets']}\nPrior: {state.get('prior', None)}\n\nPlease categorize this file using the route_file tool."

    # A message sent to Claude is a list of "content blocks", each one
    # either a chunk of text or an embedded file. Here the file excerpt
    # from extract_node (a PDF/text block) is followed by one more text
    # block -- the actual instruction -- so Claude receives "here's the
    # file, now classify it" as a single combined message.
    messages = [
        {"role": "user", "content": extracted_content + [{"type": "text", "text": prompt}]}
    ]

    try:
        # `tool_choice={"type": "tool", "name": "route_file"}` is what FORCES
        # Claude to answer using the ROUTER_TOOL schema defined above,
        # instead of replying with free-form sentences.
        response = client.messages.create(
            model=model,
            max_tokens=50 if is_dev else 1024,
            messages=messages,
            tools=[ROUTER_TOOL],
            tool_choice={"type": "tool", "name": "route_file"}
        )

        usage = response.usage
        if tracker is not None:
            tracker.record(model, response.usage)

        # Claude's reply is also a list of content blocks; because the call
        # above forced a single tool use, there should be exactly one
        # 'tool_use' block here, and `block.input` is its structured answer
        # (already validated against ROUTER_TOOL's schema) -- no text
        # parsing needed.
        matches = []
        for block in response.content:
            if block.type == 'tool_use' and block.name == 'route_file':
                matches = block.input.get('matches', [])
                break

        return {'matches': matches, 'limited': False, 'model_used': model}
    except Exception as e:
        err_info = classify_api_error(e)
        logger.error(f"API Error in triage_node: {err_info.get('reason', str(e))}")
        if err_info.get('retry'):
            return {'matches': [], 'limited': True, 'model_used': model}
        return {'matches': [], 'limited': False, 'model_used': model}

def reconcile_node(state: Stage1State) -> dict:
    """Step 3 of 3. Plain Python, no AI call: clean up Claude's raw answer
    from triage_node before handing it back to whoever asked for this
    file to be routed. Drops any entry missing a subject or chapter
    number (a malformed answer shouldn't silently misfile a real file),
    and passes the "API was unavailable" signal straight through
    unchanged so the caller knows to fall back to filing by filename
    alone rather than trusting an empty result."""
    matches = state.get('matches', [])
    buckets = state.get('buckets', [])
    
    if state.get('limited'):
        return {'matches': matches, 'limited': True, 'model_used': state['model_used']}
        
    filtered_matches = []

    # Keep only entries that actually name BOTH a subject and a chapter
    # number -- anything missing either one is a malformed answer and gets
    # silently dropped rather than risking a misfiled document.
    for match in matches:
        subj = match.get('subject')
        chap = match.get('chapter_no')
        if subj and chap is not None:
            filtered_matches.append(match)

    return {'matches': filtered_matches, 'limited': state.get('limited', False), 'model_used': state.get('model_used', '')}

def build_stage1_graph():
    """Wire up the three-step flowchart described at the top of this file:
    extract -> triage -> reconcile -> end. Returns an un-compiled graph
    object; route_one_file (below) is what actually compiles and runs it
    for one specific file."""
    graph = StateGraph(Stage1State)
    graph.add_node('extract', extract_node)
    graph.add_node('triage', triage_node)
    graph.add_node('reconcile', reconcile_node)
    graph.set_entry_point('extract')
    graph.add_edge('extract', 'triage')
    graph.add_edge('triage', 'reconcile')
    graph.add_edge('reconcile', END)
    return graph

def route_one_file(path: Path, buckets: dict | list, prior: dict | None = None,
                    tracker: TokenTracker = None) -> tuple[list[tuple], bool, str]:
    """The single entry point other code calls to classify one file --
    runs the whole extract -> triage -> reconcile flowchart for `path` and
    returns a plain, simple answer.

    Drop-in replacement for llm_route() (the old, single-call version in
    stage1_api.py): same inputs (including the optional `tracker`,
    matching llm_route's own signature), same return shape --
    (matches, limited, model_used) -- so callers don't need to know or
    care which implementation actually produced the answer.

    `tracker`, if given, is the caller's shared TokenTracker -- triage_node
    (the only step here that calls Claude) records this call's usage onto
    it, the same way dispatch.py's other two Stage 1 implementations
    (legacy, subprocess) already do. Left as None, no usage is recorded for
    this call anywhere (see triage_node's docstring).

    Each entry in `matches` is the SAME 6-tuple shape llm_route's own
    _parse_matches() produces -- (full_subject, chapter_no, chapter_name,
    role, confidence, reason) -- specifically because the calling code
    (stage1_api.py's Stage A/B loops) reads matches by
    POSITION (m[0], m[1], m[4], ...), the same way regardless of which
    router produced them.
    """
    graph = build_stage1_graph()
    # "Compiling" a graph turns the node/edge description built by
    # build_stage1_graph() into something that can actually be run.
    compiled = graph.compile()

    # `buckets` is the caller's master list of valid "subject + chapter"
    # bins, keyed by (subject, chapter_no) when it's a dict. Claude only
    # needs to see plain bin NAMES (e.g. "Physics Chapter 3") to choose
    # from -- the dict's richer (subject, chapter_no) keys are converted
    # back out of Claude's answer later, near the bottom of this function.
    if isinstance(buckets, dict):
        buckets_list = [f"{k[0]} Chapter {k[1]}" if isinstance(k, tuple) else str(k) for k in buckets.keys()]
    else:
        buckets_list = list(buckets)

    initial_state = {
        'file_path': str(path),
        'buckets': buckets_list,
        'prior': prior,
        'extracted_content': None,
        'matches': [],
        'limited': False,
        'model_used': '',
        'tracker': tracker,
    }

    final_state = compiled.invoke(initial_state)
    limited = final_state.get('limited', False)
    model_used = final_state.get('model_used', '')

    # Convert Claude's raw answer (a list of {subject, chapter_no, ...}
    # dicts) into the 6-tuple shape the caller expects -- see the
    # docstring above for why this conversion exists at all.
    matches_out = []
    if not limited and isinstance(buckets, dict):
        for item in final_state.get('matches', []):
            try:
                full = str(item["subject"]).strip()
                chno = int(item["chapter_no"])
                conf = float(item.get("confidence", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if (full, chno) in buckets:
                role = str(item.get("role") or "").strip() or None
                reason = str(item.get("reason") or "").strip()
                matches_out.append((full, chno, buckets[(full, chno)], role, conf, reason))

    return (matches_out, limited, model_used)

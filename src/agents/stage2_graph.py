"""
stage2_graph.py -- Stage 2 (Generation): the flowchart that actually turns
one chapter's class transcripts into a finished study-notes .docx. This is
the direct replacement for the OLD single-call design in
src/stage2_api.py (still present, untouched -- see dispatch.py
for how the choice between old and new is made).

THE FLOWCHART, END TO END
----------------------------
    ingest --> author --> figure --> compiler --> qa --(pass)--> DONE
                  ^                                 |
                  |________________(retry)__________|
                                                      |
                                                   (fail, too many
                                                    retries or a real
                                                    error) --> DONE, but
                                                                as a failure

  1. ingest   -- plain Python, no AI call. Prepares the raw transcript/
                 reference PDFs so Author can efficiently read them (text
                 extraction, or converting scanned pages to cropped
                 images). See tool_ingest in tools.py.
  2. author   -- the one creative-writing agent (usually the strongest,
                 most expensive model). Reads the ingested material and
                 writes out content.json (the chapter's text/structure)
                 and figures.json (descriptions of any diagrams needed).
  3. figure   -- a cheap agent whose only job is: turn figures.json into
                 actual PNG images, and check that they rendered sensibly.
  4. compiler -- another cheap agent whose only job is: turn content.json
                 + the rendered figures into the final Word document.
  5. qa       -- NOT an agent at all -- see qa_node, below. Runs a battery
                 of deterministic pass/fail checks (does the JSON match the
                 required structure? do the worked-example answers actually
                 check out mathematically? does the finished Word document
                 open correctly?) and decides, in plain code, whether the
                 chapter is actually finished.

WHAT HAPPENS IF QA SAYS "NOT GOOD ENOUGH YET"
--------------------------------------------------
route_after_qa (below) sends the flowchart back to the "author" step, with
QA's findings available for Author to read (see tool_read_qa_feedback in
tools.py) and fix. This can repeat up to config.QA_MAX_RETRY_LOOPS times
(default 5) before the flowchart gives up and reports failure -- this stops
a chapter that can never quite pass QA from looping forever.

run_stage2_chapter(), at the bottom of this file, is the single function
other code calls to run this entire flowchart for one chapter and get back
a plain success/failure/retry-later result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from langgraph.graph import StateGraph, END

import settings as config
from src.func_tools_and_utils import (
    logger, TokenTracker, write_retry_epoch,
    EXIT_OK, EXIT_RATE_LIMITED, EXIT_FATAL,
    is_ignorable
)
# Shared with src/claude_cli_subprocess/stage2_cli.py -- see
# src/common/skill_retro.py's module docstring.
from src.common.skill_retro import capture_retro_findings, apply_retro_fixes
from src.common.web_enrichment import write_web_sources_manifest
from src.claude_cli_subprocess.stage2_cli import _chapter_workspace
from src.agents.base import Stage2State, make_agent_node, get_checkpointer
from src.agents.prompts import (
    author_system_prompt, figure_system_prompt, compiler_system_prompt,
    ensure_skill_extracted
)
from src.agents.tools import (
    tool_ingest, tool_read_source, tool_view_source_page,
    tool_write_content_json, tool_write_figures_json, tool_read_qa_feedback,
    tool_figbuild, tool_view_figure, tool_compile_docx,
    tool_run_structural_gates, tool_run_quality_gates, tool_run_document_qa,
    AUTHOR_TOOLS, FIGURE_TOOLS, COMPILER_TOOLS, TOOL_REGISTRY,
    WEB_ENRICHMENT_TOOLS
)

def ingest_node(state: Stage2State) -> dict:
    """Flowchart step 1 (see the diagram at the top of this file). Plain
    Python, no AI call: hands every transcript and supporting file to
    tool_ingest (tools.py) to prepare it for Author to read efficiently,
    then returns the state unchanged (ingest doesn't need to add or change
    any of the shared clipboard fields -- it just has a side effect of
    writing prepared files to disk that Author will read later).
    In DEV_TOKEN_SAVER_MODE (the cheap smoke-test toggle), this whole step
    is skipped -- see src/agents/__init__.py."""
    chapter_dir = state['chapter_dir']
    
    if getattr(config, 'DEV_TOKEN_SAVER_MODE', False):
        logger.info("[ingest_node] DEV_TOKEN_SAVER_MODE active, mocking ingest.")
        return state
        
    for file in state.get('transcripts', []):
        tool_ingest(file, chapter_dir)
        logger.info(f"[ingest_node] Ingested transcript: {file}")
        
    for file in state.get('supporting', []):
        tool_ingest(file, chapter_dir)
        logger.info(f"[ingest_node] Ingested supporting file: {file}")
        
    return state

# The three lines below each BUILD one agent, using the shared "agent
# factory" from base.py (see make_agent_node's own docstring there for
# full detail on what it does). Each one gets its own model, its own
# narrow tool list (from tools.py), and its own job instructions
# (from prompts.py) -- this is the actual place where "one big agent with
# every tool" (the old design) becomes "several small, focused agents"
# (this new design). `max_agent_turns` caps how many back-and-forth
# steps (AI reply -> run a tool -> AI reply again -> ...) that one agent
# is allowed before it's forced to stop, as a safety net against an agent
# looping forever without finishing its job.

# Author gets the most turns (25) and, by default, the strongest/most
# expensive model (config.GENERATOR_MODEL) -- writing the actual chapter
# content is the one genuinely creative, open-ended step in this
# flowchart, so it needs the most room and the most capable model.
author_tools_list = list(AUTHOR_TOOLS)
if getattr(config, 'ENABLE_WEB_ENRICHMENT', False):
    author_tools_list.extend(WEB_ENRICHMENT_TOOLS)

author_node = make_agent_node(
    node_name='author',
    model_name=getattr(config, 'AUTHOR_MODEL', config.GENERATOR_MODEL),
    tools_for_anthropic=author_tools_list,
    system_prompt_fn=author_system_prompt,
    max_agent_turns=25,
    tool_registry=TOOL_REGISTRY
)

# Figure's job (turn figures.json into real PNG images) is narrow and
# mechanical, so it gets a cheap model (claude-haiku-4-5 by default) and
# fewer allowed turns.
figure_node = make_agent_node(
    node_name='figure',
    model_name=getattr(config, 'FIGURE_MODEL', 'claude-haiku-4-5'),
    tools_for_anthropic=FIGURE_TOOLS,
    system_prompt_fn=figure_system_prompt,
    max_agent_turns=10,
    tool_registry=TOOL_REGISTRY
)

# Compiler's job (assemble the final .docx from content.json + rendered
# figures) is the most mechanical of all three, so it gets the fewest
# turns of any agent in this flowchart.
compiler_node = make_agent_node(
    node_name='compiler',
    model_name=getattr(config, 'COMPILER_MODEL', 'claude-haiku-4-5'),
    tools_for_anthropic=COMPILER_TOOLS,
    system_prompt_fn=compiler_system_prompt,
    max_agent_turns=5,
    tool_registry=TOOL_REGISTRY
)

def qa_node(state: Stage2State) -> dict:
    """Flowchart step 5 (see the diagram at the top of this file).

    IMPORTANT: this is plain Python, NOT an AI agent -- there is no model
    call anywhere in this function, on purpose (see the "5. QA tools"
    section header comment in tools.py for why). It simply runs three
    groups of deterministic checks against whatever Author/Figure/Compiler
    produced, adds up whether they all passed, and writes that verdict to
    qa_report.json on disk (so a human, or Author on its next retry
    attempt, can see exactly what failed and why):
      - structural gates: does content.json/figures.json match the
        required shape? does every worked example's numeric answer
        actually check out?
      - quality gates: does the writing hit the project's teaching-quality
        bar (enough worked examples, not too dense, etc.)?
      - document QA: does the actual compiled .docx open correctly, with
        working diagrams and no formatting corruption?

    This check running for real, even in DEV_TOKEN_SAVER_MODE, is
    deliberate -- it's what proves the whole retry loop actually works
    (see the docstrings on tool_run_structural_gates and friends in
    tools.py): dummy placeholder content is EXPECTED to fail these checks.

    Returns three updates to the shared clipboard: the qa_report itself,
    an incremented retry counter (qa_pass_count -- despite the name, this
    counts ATTEMPTS, not successes), and the flowchart's new status --
    'done' only if every single check passed, otherwise 'running' (meaning:
    not finished yet, more work may be needed).

    If an earlier node (author/figure/compiler) already set a terminal
    failure status (failed_retryable/failed_fatal/failed_turns_exhausted),
    that status is passed straight through instead of being overwritten --
    qa runs after every node on this flowchart's fixed edges regardless of
    what happened upstream, so without this check a real upstream failure
    (e.g. a bad API key, or an agent that ran out of its turn budget with
    incomplete content.json) would silently be replaced by whatever qa's
    own gates conclude, letting route_after_qa's status check (below)
    never actually see it."""
    existing_status = state.get('status')
    if existing_status in ('failed_retryable', 'failed_fatal', 'failed_turns_exhausted'):
        return {'status': existing_status}

    chapter_dir = state['chapter_dir']
    content_json_path = str(Path(chapter_dir) / 'content.json')
    figures_json_path = str(Path(chapter_dir) / 'figures.json')
    docx_path = str(Path(chapter_dir) / 'output.docx')
    
    # Each tool_run_*_gates() call below runs its own group of pass/fail
    # checks and returns a dict of individual results -- none of these
    # calls involve the AI; they're plain deterministic checks (does the
    # file match the required shape? does the math check out? etc.).
    structural = tool_run_structural_gates(content_json_path, figures_json_path)
    quality = tool_run_quality_gates(content_json_path, chapter_dir)
    doc_qa = tool_run_document_qa(docx_path, content_json_path)

    # A "gate" only counts as OK if every check inside it passed -- one
    # failure anywhere in the group fails the whole group.
    structural_ok = (
        structural.get('schema_ok', False) and
        structural.get('verify_ok', False) and
        structural.get('invariants_ok', False)
    )
    quality_ok = (
        quality.get('pedagogy_ok', False) and
        quality.get('baseline_ok', False)
    )
    doc_ok = doc_qa.get('ok', False)

    qa_report = {
        'structural': structural,
        'quality': quality,
        'doc_qa': doc_qa,
        'all_passed': structural_ok and quality_ok and doc_ok,
    }

    qa_report_path = Path(chapter_dir) / 'qa_report.json'
    try:
        with open(qa_report_path, 'w', encoding='utf-8') as f:
            json.dump(qa_report, f, indent=2)
    except Exception as e:
        logger.error(f"[qa_node] Failed to write qa_report.json: {e}")

    if not qa_report['all_passed']:
        logger.warning(f"[qa_node] QA gates failed for {chapter_dir}")
        logger.warning(f"  structural_ok={structural_ok} (schema={structural.get('schema_ok')}, verify={structural.get('verify_ok')}, invariants={structural.get('invariants_ok')})")
        logger.warning(f"  quality_ok={quality_ok} (pedagogy={quality.get('pedagogy_ok')}, baseline={quality.get('baseline_ok')})")
        logger.warning(f"  doc_ok={doc_ok}")
        if structural.get('failures'):
            logger.warning(f"  blocking failures: {structural['failures'][:3]}")

    return {
        'qa_report': qa_report,
        'qa_pass_count': state.get('qa_pass_count', 0) + 1,
        'status': 'done' if qa_report['all_passed'] else 'running'
    }

def route_after_qa(state: Stage2State) -> Literal['pass', 'retry', 'fail']:
    """The CONDITIONAL EDGE that runs right after the qa step (see
    build_stage2_graph, below, and the "WHAT IS A GRAPH" section of
    src/agents/__init__.py for what a conditional edge is). This is the
    one place in the whole flowchart where "what happens next" depends on
    an outcome rather than always being the same fixed next step -- it
    decides, in order:
      1. If an earlier step hit a real error (an API failure that
         classify_api_error judged either retryable-later or a dead end,
         or an agent that ran out of its turn budget without finishing):
         stop, and report that failure outward.
      2. If every QA check passed: stop, successfully.
      3. If QA failed, but we haven't yet used up all
         config.QA_MAX_RETRY_LOOPS attempts (default 5): go back to the
         "author" step so it can read QA's feedback and try again.
      4. Otherwise (QA failed, and we're out of retries): stop, as a
         failure -- this is what stops an unfixable chapter from looping
         forever.
    The three possible answers this function can return -- 'pass',
    'retry', 'fail' -- are exactly the three arrows drawn out of the "qa"
    box in build_stage2_graph's flowchart, below.
    """
    if state.get('status') in ('failed_retryable', 'failed_fatal', 'failed_turns_exhausted'):
        return 'fail'

    qa_report = state.get('qa_report', {})
    if qa_report.get('all_passed', False):
        return 'pass'

    max_loops = getattr(config, 'QA_MAX_RETRY_LOOPS', 5)
    if state.get('qa_pass_count', 0) < max_loops:
        return 'retry'

    return 'fail'

def build_stage2_graph():
    """Draw the actual flowchart described at the top of this file: five
    named boxes (nodes), wired together with arrows (edges) that say what
    runs next. Returns an un-compiled graph description; run_stage2_chapter
    (below) is what actually compiles it (attaches the checkpoint database)
    and runs it for one real chapter.

    Reading this function IS reading the flowchart:
      - set_entry_point('ingest')            -- start here.
      - add_edge('ingest', 'author')         -- ingest always leads to author.
      - add_edge('author', 'figure')         -- author always leads to figure.
      - add_edge('figure', 'compiler')       -- figure always leads to compiler.
      - add_edge('compiler', 'qa')           -- compiler always leads to qa.
      - add_conditional_edges('qa', route_after_qa, {...}) -- after qa,
        the NEXT step depends on route_after_qa's answer (see its own
        docstring above): 'pass' or 'fail' both end the run (END is
        LangGraph's built-in "stop here" marker); 'retry' loops back to
        'author' to try again with QA's feedback in hand.
    """
    graph = StateGraph(Stage2State)
    graph.add_node('ingest', ingest_node)
    graph.add_node('author', author_node)
    graph.add_node('figure', figure_node)
    graph.add_node('compiler', compiler_node)
    graph.add_node('qa', qa_node)

    graph.set_entry_point('ingest')
    graph.add_edge('ingest', 'author')
    graph.add_edge('author', 'figure')
    graph.add_edge('figure', 'compiler')
    graph.add_edge('compiler', 'qa')
    graph.add_conditional_edges('qa', route_after_qa, {
        'pass': END,
        'retry': 'author',
        'fail': END,
    })
    return graph

# -----------------------------------------------------------------------------
# RETRO / WEB ENRICHMENT HELPERS
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# GRAPH RUNNER
# -----------------------------------------------------------------------------

def run_stage2_chapter(chapter_dir, live_mode: bool, resume: bool = True) -> int:
    """THE single entry point other code calls to run Stage 2's entire
    flowchart for one chapter, from start to finish, and get back a plain
    result code. This is the direct equivalent of the old design's
    run_generate() in stage2_api.py, and returns the exact same
    three possible exit codes so the rest of the pipeline (main.py, the
    nightly wrapper script, etc.) doesn't need to know or care which
    implementation actually ran:
        EXIT_OK (0)             -- finished successfully (a real .docx exists).
        EXIT_RATE_LIMITED (42)  -- hit a retryable error (e.g. rate limit);
                                   safe to try again later.
        EXIT_FATAL (1)          -- failed for good this run (QA never
                                   passed after all retries, a
                                   non-retryable error occurred, or an
                                   agent exhausted its turn budget without
                                   finishing).

    Args:
        chapter_dir: which chapter's folder to work on. If left empty/None,
                     this function picks the next chapter itself (the same
                     "find one folder with input files and no output yet"
                     logic the old design uses -- see
                     select_target_chapter in stage2_api.py).
        live_mode: if False ("mock mode"), do nothing and report success
                     immediately, WITHOUT spending any API calls -- this is
                     what lets the nightly automation do a free, zero-cost
                     dry run every night by default.
        resume: if True, pick a crashed/interrupted chapter back up from
                     its last saved checkpoint instead of starting over
                     (see get_checkpointer in base.py).
    """
    if not chapter_dir:
        from src.direct_api.stage2_api import select_target_chapter
        chapter_dir = select_target_chapter(config.DEFAULT_TARGET_ROOT)
        if not chapter_dir:
            logger.info("No chapter folder ready for note generation. Nothing to do.")
            return EXIT_OK
    # Defensive: main.py always passes a Path, but coerce here too in case
    # some other caller (or a test) hands this a plain string -- cheap and
    # a no-op if it's already a Path.
    chapter_dir = Path(chapter_dir)

    if not chapter_dir.is_dir():
        # Only reachable via an explicit --target-dir -- see the matching
        # check/comment in direct_api.stage2_api.run_generate().
        logger.error(f"--target-dir path does not exist or is not a directory: {chapter_dir}")
        return EXIT_FATAL

    if not live_mode:
        # Mock mode: report success without touching the API or the
        # flowchart at all -- see the Args note above.
        logger.info(f"Mock mode enabled for {chapter_dir}. Returning EXIT_OK.")
        return EXIT_OK

    chapter_dir_str = str(chapter_dir)
    ensure_skill_extracted()  # make sure the skill's scripts exist as real files (see prompts.py)

    # Gather this chapter's own input files, ready to hand to the ingest
    # step. "spine" transcripts (actual class lectures) live in
    # transcripts/; optional extra reference material lives in supporting/.
    transcripts_dir = Path(chapter_dir_str) / getattr(config, 'TRANSCRIPTS_DIR', 'transcripts')
    transcripts = [str(f) for f in transcripts_dir.glob('*') if f.is_file() and not is_ignorable(f)]

    sup_dir = Path(chapter_dir_str) / getattr(config, 'SUP_DIR', 'supporting')
    supporting = []
    if sup_dir.exists():
        supporting = [str(f) for f in sup_dir.glob('*') if f.is_file() and not is_ignorable(f)]

    # This is the STARTING VALUE of the shared clipboard (Stage2State) that
    # gets passed from node to node through the whole flowchart below.
    initial_state = {
        'chapter_dir': chapter_dir_str,
        'transcripts': transcripts,
        'supporting': supporting,
        'content_json': None,
        'figures_json': None,
        'qa_report': None,
        'qa_pass_count': 0,
        'turn_count': 0,
        'attempt_count': 0,
        'messages': {},
        'status': 'running',
    }

    graph = build_stage2_graph()

    # "thread_id" is how LangGraph's checkpoint database knows which saved
    # run to resume -- one chapter, one thread, one save file (see
    # get_checkpointer in base.py).
    thread_config = {'configurable': {'thread_id': Path(chapter_dir_str).name}}

    try:
        # Open this chapter's checkpoint database, "compile" the flowchart
        # (turn the description built by build_stage2_graph into something
        # that can actually run), and run it end-to-end -- automatically
        # resuming from the last saved point if one exists for this
        # chapter, or starting fresh otherwise.
        with get_checkpointer(chapter_dir_str) as checkpointer:
            compiled = graph.compile(checkpointer=checkpointer)
            final_state = compiled.invoke(initial_state, config=thread_config)

    except Exception as e:
        # Something broke outside any individual node's own error handling
        # (e.g. the checkpoint database itself couldn't be opened) --
        # treat the whole chapter as failed for this run.
        logger.exception(f"Error executing stage 2 graph: {e}")
        return EXIT_FATAL

    status = final_state.get('status', 'running')

    if status == 'done':
        # Mirror legacy's explicit expected_docx.exists() guard (stage2_api.py)
        # before trusting the graph's own "done" signal -- the graph's internal status
        # must never be the sole authority that a real .docx was actually produced.
        docx_produced = any(Path(chapter_dir_str).glob('*.docx'))
        if not docx_produced:
            logger.error(
                f"[run_stage2_chapter] QA reported all_passed but no .docx exists in "
                f"{chapter_dir_str}; refusing to mark done, writing failure marker instead."
            )
            fail_marker_path = Path(chapter_dir_str) / config.FAILMARK
            fail_marker_path.write_text(
                "Stage 2 (graph) reported QA success but no .docx was found on disk.\n",
                encoding="utf-8",
            )
            return EXIT_FATAL

        # Post-run web-sources manifest + retrospective capture/auto-fix.
        # Gated on a PROVEN-successful chapter (status done AND a real .docx),
        # mirroring claude_cli_subprocess.stage2_cli's own
        # `if result["ok"] and expected_docx.exists()` gate -- a failed or
        # atypical run must never drive an autonomous skill mutation. Placed
        # OUTSIDE the generation try/except above so a stray error in this
        # best-effort post-processing can never flip an already-successful
        # chapter to EXIT_FATAL (each helper is also internally best-effort).
        target_dir = Path(chapter_dir)
        # Where THIS chapter's .study-notes/run.jsonl actually lives -- a
        # local scratch workspace, not target_dir itself (which is normally
        # Drive-synced). See src/agents/tools.py's _skill_run_env, which
        # scopes every skill-script subprocess call to this same folder.
        chapter_workspace = _chapter_workspace(target_dir)
        try:
            if getattr(config, 'ENABLE_WEB_ENRICHMENT', False):
                write_web_sources_manifest(target_dir, final_state.get('web_sources', []))

            retro = capture_retro_findings(target_dir, chapter_workspace,
                                            resolved_skill_dir=Path(ensure_skill_extracted()))
            if retro["ok"] and retro["candidates"] and getattr(config, 'ENABLE_AUTO_SKILL_IMPROVEMENT', False):
                apply_retro_fixes(retro["candidates"], chapter_workspace)
        except Exception as e:
            logger.warning(f"Post-run retro/manifest step failed (chapter still OK): {e}")

        marker_path = Path(chapter_dir_str) / config.MARKER
        marker_path.touch()
        return EXIT_OK
    elif status == 'failed_retryable':
        # Code 42, picked up by the Windows/WSL wrapper scripts to schedule
        # an automatic retry later, instead of writing a permanent failure
        # marker -- see docs/migration-to-agents.md's Verification Plan for
        # the exit-code contract this must keep matching.
        return EXIT_RATE_LIMITED
    else:
        fail_marker_path = Path(chapter_dir_str) / config.FAILMARK
        fail_marker_path.touch()
        return EXIT_FATAL

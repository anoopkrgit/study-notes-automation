"""
dispatch.py -- the ON/OFF SWITCH between the old, untouched, single-call
code and this folder's new multi-agent flowcharts.

WHY THIS FILE EXISTS
----------------------
Rather than replace the old code outright, this project keeps BOTH
versions working side by side: the old code (src/func_generate_notes.py
and src/func_assemble_chapters.py) is left completely unchanged, and every
new "agent" implementation lives only in this src/agents/ folder. The two
functions below are the only place in the whole project that decides,
each time Stage 1 or Stage 2 needs to run, WHICH of the two versions
actually does the work.

That decision is controlled by two independent settings in
config/settings.py (each read from an environment variable, defaulting to
"legacy" so nothing changes unless someone deliberately opts in):
    config.STAGE1_IMPL  ("legacy" or "graph")  -- controls route_file, below
    config.STAGE2_IMPL  ("legacy" or "graph")  -- controls generate_notes, below

WHY THIS MATTERS FOR SAFETY
------------------------------
Because the old code is never modified, flipping either setting back to
"legacy" is an instant, risk-free rollback at any time -- there is no
"undo a code change" involved, just changing which of two already-working
implementations gets called next. This is also what makes it possible to
run the exact same real chapter through BOTH implementations and directly
compare their output (see the Verification Plan in
docs/migration-to-agents.md).

The rest of the codebase (main.py, in particular) calls the two functions
below by name, and never needs to import the old or new implementation
modules directly -- it just asks this file to "generate the notes" or
"route this file", and doesn't need to know or care which actual code path
ran.
"""

from __future__ import annotations

from pathlib import Path

import settings as config
from src.func_tools_and_utils import logger


def generate_notes(target_dir: Path = None, live_mode: bool = False, verbose: bool = False, impl: str = None) -> int:
    """Stage 2's switch. Same inputs and return value as the old
    run_generate() in func_generate_notes.py (a plain integer exit code --
    see run_stage2_chapter's docstring in stage2_graph.py for exactly what
    each code means), so main.py's call site doesn't need to change
    depending on which implementation actually runs.

    `impl`, if given ("legacy" or "graph"), is authoritative and comes from
    main.py's `--stage2-impl` CLI flag -- the real entrypoint always passes
    it explicitly, so which implementation runs is a CLI choice all the way
    from the shell wrapper down to here, not something that requires
    editing this file or an env var. `impl=None` (the default) falls back
    to config.STAGE2_IMPL, kept only so callers that don't care (tests,
    ad-hoc scripts) can still rely on the env-var default.
    """
    impl = impl if impl is not None else getattr(config, 'STAGE2_IMPL', 'legacy')
    if impl == 'graph':
        logger.info('Stage 2: using GRAPH implementation (multi-agent)')
        from src.agents.stage2_graph import run_stage2_chapter
        return run_stage2_chapter(target_dir, live_mode)
    else:
        logger.info('Stage 2: using LEGACY implementation (monolithic loop)')
        from src.func_generate_notes import run_generate
        return run_generate(target_dir, live_mode, verbose)


def route_file(path: Path, buckets, prior=None, tracker=None, impl: str = None):
    """Stage 1's switch. Same inputs and return value as the old
    llm_route() in func_assemble_chapters.py: a (matches, limited,
    model_used) tuple -- see route_one_file's docstring in
    stage1_graph.py for exactly what each of those means.

    `impl`, if given ("legacy" or "graph"), is authoritative and comes from
    main.py's `--stage1-impl` CLI flag by way of run_assemble()'s
    stage1_impl parameter -- a CLI choice threaded all the way down to
    both of func_assemble_chapters.py's routing call sites, not something
    that requires editing this file or an env var. `impl=None` (the
    default) falls back to config.STAGE1_IMPL, kept only so callers that
    don't care (tests, ad-hoc scripts) can still rely on the env-var
    default.
    """
    impl = impl if impl is not None else getattr(config, 'STAGE1_IMPL', 'legacy')
    if impl == 'graph':
        logger.info('Stage 1: using GRAPH implementation (multi-agent)')
        from src.agents.stage1_graph import route_one_file
        return route_one_file(path, buckets, prior)
    else:
        logger.info('Stage 1: using LEGACY implementation')
        from src.func_assemble_chapters import llm_route
        return llm_route(path, buckets, prior, tracker)

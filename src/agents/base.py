"""
base.py -- the shared engine that runs ONE agent for a few conversation
"turns", used by every specialised agent in stage2_graph.py (Author,
Figure, Compiler). Read this file first, before any other file in this
folder; it defines the building block everything else is made of.

WHAT THIS FILE PROVIDES
-----------------------
1. Stage2State / Stage1State: plain data-holders (see "What is a TypedDict"
   below) describing everything the Stage 2 / Stage 1 flowchart carries
   from one step to the next.
2. make_agent_node(...): a "factory function" -- a function whose job is
   to BUILD another function. You call make_agent_node() once per agent
   (Author, Figure, Compiler -- see stage2_graph.py), describing that
   agent's model, tools, and job instructions, and it hands back a ready-
   to-use node function that stage2_graph.py's flowchart can call. This
   avoids writing the same "talk to Claude, run its tool requests, repeat"
   loop three separate times by hand.
3. get_checkpointer(...): opens the save-file (a small local database)
   that lets a run resume from where it left off if the process is
   killed or crashes partway through a chapter (see "What is a
   checkpoint" below).

WHAT IS A TypedDict (Stage2State / Stage1State, below)
---------------------------------------------------------
A "dict" (dictionary) in Python is a simple lookup table of named values,
e.g. {"chapter_dir": "/some/path", "status": "running"}. A TypedDict is
just a dict with a fixed, documented list of expected field names and
types written down in advance, purely so a human reader (and some
checking tools) can see at a glance what fields the state is supposed to
carry, without changing how it behaves at runtime. Stage2State is the
"clipboard" passed from node to node through the Stage 2 flowchart (see
stage2_graph.py); Stage1State is the equivalent for Stage 1 (see
stage1_graph.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict, Literal, Callable, Any

import anthropic
from langgraph.checkpoint.sqlite import SqliteSaver

import settings as config
from src.func_tools_and_utils import (
    logger,
    classify_api_error,
    TokenTracker,
    write_retry_epoch,
    EXIT_OK,
    EXIT_RATE_LIMITED,
    EXIT_FATAL
)

class Stage2State(TypedDict):
    """Everything the Stage 2 (note-generation) flowchart carries between
    its steps -- ingest, author, figure, compiler, qa (see stage2_graph.py).
    Each field below is one "slot" on that shared clipboard.
    """
    chapter_dir: str            # folder on disk holding this chapter's files
    transcripts: list[str]      # paths to the class-lecture PDFs (the "spine")
    supporting: list[str]       # paths to extra reference material (optional)
    content_json: dict | None   # the chapter's text/structure, once Author writes it
    figures_json: dict | None   # the chapter's diagram specs, once Author writes them
    qa_report: dict | None      # the most recent quality-check results (see qa_node)
    qa_pass_count: int          # how many times QA has run so far (bounds the retry loop)
    turn_count: int             # how many Claude round-trips have happened, in total
    attempt_count: int          # reserved for cross-process-restart retry counting
    web_searches: int           # how many web searches have been performed in this chapter
    web_fetches: int            # how many web pages have been fetched in this chapter
    web_sources: list[str]      # URLs fetched successfully during this chapter's generation
    messages: dict[str, list]   # each agent's own conversation history, keyed by
                                 # agent name (e.g. messages["author"]) -- kept
                                 # separate so Author/Figure/Compiler don't see
                                 # each other's turn-by-turn back-and-forth, only
                                 # the final files each one produces
    status: Literal['running', 'done', 'failed_retryable', 'failed_fatal',
                     'failed_turns_exhausted']
                                 # the flowchart's current verdict: still going,
                                 # finished successfully, failed but worth
                                 # retrying later (e.g. rate limit), failed
                                 # for good (e.g. bad API key), or an agent
                                 # ran out of its turn budget without ever
                                 # reaching a terminal stop_reason

class Stage1State(TypedDict):
    """Everything the Stage 1 (Triage/Extraction) flowchart carries between
    its steps -- extract, triage, reconcile (see stage1_graph.py). This
    flowchart runs once PER incoming file, not once per chapter.
    """
    file_path: str                     # the one file being classified right now
    buckets: list[str]                 # the list of valid "subject + chapter" bins
                                        # this file could be filed into
    prior: dict | None                 # a guess at the right bin based on the
                                        # filename alone, to be confirmed or
                                        # overridden by actually reading the file
    extracted_content: list[dict] | None  # the file's content, prepared for Claude
                                           # to read (see extract_node)
    matches: list[dict]                # Claude's raw routing decision(s)
    limited: bool                      # True if the API was unavailable/rate-limited
                                        # for this file (caller should fall back to
                                        # filing by filename alone)
    model_used: str                    # which model actually answered, for logging
    tracker: TokenTracker | None       # caller's shared cost/token tracker, if any --
                                        # passed through so triage_node's API call gets
                                        # recorded against the caller's own totals
                                        # instead of a throwaway local tracker (see
                                        # route_one_file/triage_node in stage1_graph.py)

def make_agent_node(
    node_name: str,
    model_name: str,
    tools_for_anthropic: list[dict],
    system_prompt_fn: Callable[[Any], Any],
    max_agent_turns: int = 25,
    tool_registry: dict[str, Callable] = None
) -> Callable[[Stage2State], dict]:
    """Build one flowchart node that runs a single Claude-based agent for up
    to `max_agent_turns` conversation turns.

    Think of this as a template: "make me an Author agent", "make me a
    Figure agent", etc. -- each call below in stage2_graph.py passes in
    that agent's own model, own tool list, and own job-instructions
    function, and gets back a ready-to-run node function.

    Inputs (what you hand this function when building an agent):
      node_name          -- short label used in logs and for keeping this
                             agent's own conversation history separate
                             (e.g. "author", "figure", "compiler").
      model_name          -- which Claude model this agent normally uses
                             (e.g. "claude-sonnet-5" for Author, a cheaper
                             model for Figure/Compiler).
      tools_for_anthropic -- the list of tool DEFINITIONS (name + expected
                             arguments) this agent is allowed to request --
                             see AUTHOR_TOOLS / FIGURE_TOOLS / COMPILER_TOOLS
                             in tools.py. Handing an agent only the tools
                             relevant to its own job (instead of every tool
                             that exists) is the main improvement over the
                             old single-agent design.
      system_prompt_fn    -- a function that, given the current state,
                             returns this agent's job instructions (its
                             "system prompt" -- see prompts.py).
      max_agent_turns     -- safety cap on how many back-and-forth turns
                             this agent may take in one run, so a confused
                             or looping agent can't run forever.
      tool_registry       -- a lookup table from tool NAME (a string) to the
                             actual Python function that performs it (see
                             TOOL_REGISTRY in tools.py) -- this is how a
                             tool request coming back from Claude ("please
                             run tool_read_source") gets turned into an
                             actual function call on our side.

    Output: a function (node_fn, below) that stage2_graph.py's flowchart
    can call directly as one of its nodes. It takes the current
    Stage2State and returns a dict of the fields it wants to update.
    """
    if tool_registry is None:
        tool_registry = {}

    def node_fn(state: Stage2State) -> dict:
        """This is the actual node function returned to the flowchart. It
        runs ONE agent (Author, Figure, or Compiler, depending on which
        call to make_agent_node produced this) through up to
        `max_agent_turns` rounds of: ask Claude what to do next, run
        whatever tool(s) it asked for, feed the result back, repeat --
        until Claude stops asking for tools (meaning it believes its part
        of the job is done) or something goes wrong."""
        # `client` is our connection to the Claude API -- every message this
        # agent sends and receives goes through it. `tracker` just counts
        # tokens used (for cost logging), it doesn't affect behaviour.
        client = anthropic.Anthropic()
        tracker = TokenTracker()

        current_model = model_name
        max_tokens = 4096   # normal cap on how long ONE Claude reply may be

        is_dev = getattr(config, 'DEV_TOKEN_SAVER_MODE', False)

        if is_dev:
            # Cost-safe smoke-test mode (see __init__.py's "DEV_TOKEN_SAVER_MODE"
            # section): force every agent onto the same cheap model, and cut
            # its reply length way down, so a full run through the whole
            # flowchart costs pennies instead of real generation-scale cost.
            current_model = getattr(config, 'FIGURE_MODEL', 'claude-haiku-4-5')
            max_tokens = 50

        # Ask this agent's own prompt-building function (see prompts.py) for
        # its job instructions. Different agents get different instructions
        # here even though they share this same node_fn code.
        system_prompt = system_prompt_fn(state)

        # Each agent keeps its OWN conversation history, stored under its own
        # name inside state['messages'] (e.g. state['messages']['author']).
        # This is deliberate: Author, Figure, and Compiler never see each
        # other's turn-by-turn back-and-forth, only the FILES each one
        # leaves behind on disk (content.json, figures.json, the .docx) --
        # keeping each agent's context small and focused on its own job.
        messages = state.get('messages', {}).get(node_name, [])
        if not messages:
            # First time this agent has ever run for this chapter: give it
            # a simple starting instruction.
            messages = [{"role": "user", "content": f"Please begin your task as {node_name}."}]
        else:
            # Resuming a previous run (e.g. after a QA retry, or a crash):
            # pick the conversation back up where it left off.
            messages = list(messages)
            if messages[-1].get("role") == "assistant":
                # To prevent Anthropic from treating this as a prefill (which crashes if
                # the dummy/previous output ended with trailing whitespace), append a new user prompt.
                messages.append({"role": "user", "content": "Please continue. If QA ran, review the QA feedback and address any issues."})

        chapter_dir = state.get('chapter_dir', '')
        chapter_basename = Path(chapter_dir).name if chapter_dir else 'unknown'

        turn_count = state.get('turn_count', 0)
        status = state.get('status', 'running')
        
        web_searches = state.get('web_searches', 0)
        web_fetches = state.get('web_fetches', 0)
        web_sources = list(state.get('web_sources', []))

        # ---------------------------------------------------------------
        # THE MAIN LOOP: one pass of this loop = one "turn" = one round
        # trip to Claude. This is the mechanical heart of every agent in
        # this project -- see the module docstring at the top of this file
        # ("WHAT IS AN AGENT") for the plain-English version of what's
        # happening here.
        # ---------------------------------------------------------------
        for _ in range(max_agent_turns):
            try:
                # Step 1: send the conversation so far to Claude, along
                # with the list of tools this agent is allowed to use.
                response = client.messages.create(
                    model=current_model,
                    system=system_prompt,
                    messages=messages,
                    tools=tools_for_anthropic if tools_for_anthropic else anthropic.NOT_GIVEN,
                    max_tokens=max_tokens
                )

                tracker.record(current_model, response.usage)
                # Remember Claude's reply as part of the conversation, so
                # the NEXT turn (if there is one) has full context.
                messages.append(response.model_dump(include={"role", "content"}))

                has_tool_use = any(getattr(b, 'type', None) == 'tool_use' for b in response.content)

                if has_tool_use:
                    # Claude wants one or more tools run. `response.content`
                    # can contain several requests at once; go through each.
                    tool_results = []
                    for content_block in response.content:
                        if getattr(content_block, 'type', None) == 'tool_use':
                            tool_name = content_block.name    # which tool, e.g. "tool_read_source"
                            tool_args = content_block.input   # the arguments Claude chose to pass it
                            tool_id = content_block.id         # links this result back to this specific request

                            # Step 2: look up the real Python function behind
                            # that tool name, and actually run it. This is the
                            # ONE place in the whole loop where something
                            # really happens on disk/network -- everything
                            # else here is just conversation bookkeeping.
                            tool_fn = tool_registry.get(tool_name)
                            if tool_fn:
                                try:
                                    if 'chapter_dir' in tool_args:
                                        # Never trust the model-supplied
                                        # chapter_dir -- it's the sandbox
                                        # root every path-scoping check in
                                        # tools.py validates against, so
                                        # letting the model set it lets it
                                        # define its own boundary. Always
                                        # use this run's real chapter_dir
                                        # instead.
                                        tool_args = {**tool_args, 'chapter_dir': chapter_dir}
                                    
                                    # Track budget for web tools
                                    if tool_name == 'tool_web_search':
                                        if web_searches >= getattr(config, 'MAX_WEB_SEARCHES_PER_CHAPTER', 15):
                                            raise Exception("Budget exceeded: Maximum web searches for this chapter reached.")
                                        web_searches += 1
                                    elif tool_name == 'tool_web_fetch':
                                        # For simplicity, fetching also counts against the global web tools budget, or its own implicit cap
                                        if web_fetches >= getattr(config, 'MAX_WEB_SEARCHES_PER_CHAPTER', 15) * 3:
                                            raise Exception("Budget exceeded: Maximum web fetches for this chapter reached.")
                                        web_fetches += 1
                                        
                                    result = tool_fn(**tool_args)
                                    
                                    if tool_name == 'tool_web_fetch' and 'url' in tool_args:
                                        if tool_args['url'] not in web_sources:
                                            web_sources.append(tool_args['url'])

                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": tool_id,
                                        "content": str(result)
                                    })
                                except Exception as e:
                                    # A tool blowing up (bad arguments, file
                                    # not found, etc.) is reported back to
                                    # CLAUDE as a tool error, not raised as a
                                    # Python exception -- this gives the agent
                                    # a chance to notice the mistake and try
                                    # something different on its next turn,
                                    # instead of crashing the whole run.
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": tool_id,
                                        "content": f"Error executing tool: {e}",
                                        "is_error": True
                                    })
                            else:
                                # Claude asked for a tool name that isn't in
                                # this agent's registry at all -- shouldn't
                                # normally happen since tools_for_anthropic
                                # only lists real, registered tools.
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": tool_id,
                                    "content": f"Unknown tool: {tool_name}",
                                    "is_error": True
                                })

                    if tool_results:
                        # Step 3: send every tool's result back to Claude as
                        # the next message, so it can see what happened and
                        # decide what to do next. The loop then repeats.
                        messages.append({
                            "role": "user",
                            "content": tool_results
                        })

                elif response.stop_reason in ('end_turn', 'stop_sequence'):
                    # Claude replied with plain text and asked for NO tools --
                    # its signal that it believes this agent's job is done
                    # for now. Stop looping; the flowchart moves on to
                    # whichever node comes next.
                    break
                else:
                    # e.g. stop_reason == 'max_tokens' with no tool_use blocks.
                    # We must append a user message so the next turn isn't treated
                    # as a prefill which would crash on trailing whitespace.
                    messages.append({
                        "role": "user",
                        "content": "Your response was truncated or ended unexpectedly. Please continue."
                    })

            except Exception as exc:
                # Something went wrong calling the API itself (rate limit,
                # network blip, bad request, etc.) -- classify_api_error
                # decides whether this is worth automatically retrying later
                # (e.g. "come back in 10 minutes") or is a dead end (e.g. bad
                # API key) that a human needs to fix.
                error_info = classify_api_error(exc)
                if error_info.get('retry'):
                    logger.warning(f"Retryable error in {node_name}: {error_info.get('reason')}")
                    if 'retry_epoch' in error_info:
                        write_retry_epoch(error_info['retry_epoch'])
                    status = 'failed_retryable'
                else:
                    logger.error(f"Fatal error in {node_name}: {error_info.get('reason')}")
                    status = 'failed_fatal'
                break
        else:
            # The loop ran out of range(max_agent_turns) iterations without
            # ever hitting a `break` above -- i.e. Claude was still asking
            # for tools (stop_reason == 'tool_use') on the very last turn,
            # with no terminal stop_reason and no exception. Left alone,
            # `status` would still read whatever it was initialized to
            # ('running'), which downstream flowchart edges would treat as
            # "still in progress" rather than a failure -- signal the
            # turn-budget cutoff explicitly instead.
            status = 'failed_turns_exhausted'

        turn_count += 1

        # ---------------------------------------------------------------
        # DEBUG DUMP: after this agent finishes its turns (however that
        # happened -- success, error, or hitting max_agent_turns), write a
        # plain, human-readable JSON snapshot of the whole state to disk.
        # This exists purely so a person can `cat` or open this file to see
        # exactly what happened, without needing to understand LangGraph's
        # own internal checkpoint format (see get_checkpointer, below,
        # which is the thing that actually resumes a crashed run -- this
        # debug file is never read back by the code, it's for humans only).
        # ---------------------------------------------------------------
        if hasattr(config, 'CHAPTER_PROGRESS_DIR') and chapter_dir:
            debug_path = Path(config.CHAPTER_PROGRESS_DIR) / f"{chapter_basename}_debug.json"
            debug_state = dict(state)

            # Fold this agent's updated conversation back into the shared
            # per-agent messages dict before dumping it.
            new_messages_dict = dict(state.get('messages', {}))
            new_messages_dict[node_name] = messages
            debug_state['messages'] = new_messages_dict
            debug_state['turn_count'] = turn_count
            debug_state['status'] = status
            debug_state['web_searches'] = web_searches
            debug_state['web_fetches'] = web_fetches
            debug_state['web_sources'] = web_sources

            try:
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                with open(debug_path, 'w', encoding='utf-8') as f:
                    json.dump(debug_state, f, indent=2, default=str)
            except Exception as e:
                logger.error(f"Failed to write debug state: {e}")

        tracker.log_summary(logger, f'Stage 2 ({node_name})')

        # This is what actually goes back to the flowchart: only the
        # fields this node wants to CHANGE. LangGraph merges this into the
        # shared Stage2State before handing it to whichever node runs next.
        return {
            "messages": {node_name: messages},
            "turn_count": turn_count,
            "status": status,
            "web_searches": web_searches,
            "web_fetches": web_fetches,
            "web_sources": web_sources
        }

    return node_fn

def get_checkpointer(chapter_dir: str) -> SqliteSaver:
    """Open (creating if needed) this chapter's save-point database.

    WHAT IS A CHECKPOINT, IN PLAIN TERMS: every time the Stage 2 flowchart
    finishes a node (ingest, author, figure, compiler, or qa), LangGraph
    automatically saves the current Stage2State to a small local database
    file (one per chapter, under state/graph-checkpoints/). If the process
    is killed, the machine loses power, or an API rate limit forces a
    pause, the NEXT run for that same chapter can pick up from the last
    completed node instead of starting the whole chapter over from
    scratch. This is the direct equivalent of the old code's habit of
    writing state/progress/<chapter>.json after every turn -- just backed
    by a proper small database instead of a hand-rolled JSON file.

    Returns a "context manager" -- a Python object meant to be used with
    a `with ... as ...:` block (see run_stage2_chapter in stage2_graph.py)
    so the database file is always closed properly, even if something
    goes wrong while it's open.
    """
    chapter_basename = Path(chapter_dir).name
    checkpoint_dir = Path(getattr(config, 'GRAPH_CHECKPOINT_DIR', Path('checkpoints')))
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    db_path = checkpoint_dir / f"{chapter_basename}.sqlite"
    return SqliteSaver.from_conn_string(str(db_path))

"""
================================================================================
 START HERE -- a map of this folder, for a reader new to Python and to "AI
 agents", before diving into any individual file.
================================================================================

WHAT PROBLEM THIS FOLDER SOLVES
--------------------------------
The project turns raw class transcripts (PDFs) into a finished, print-ready
study-notes Word document (.docx). The OLD way of doing that (still present,
untouched, in src/stage2_api.py and src/stage1_api.py)
was: one single call to Claude that was handed a big toolbox of generic
commands and told "figure out the whole job yourself, turn after turn".

This folder is the NEW way: instead of one do-everything call, the job is
split into several smaller, specialised "agents" -- each one is a separate
call to Claude, given only the small set of tools relevant to ITS one job,
run in a fixed order. Think of it like an assembly line instead of one
person doing every step.

WHAT "AGENT" MEANS HERE, IN PLAIN TERMS
----------------------------------------
Claude (the AI model) cannot directly read a file, write a file, or run a
program by itself -- talking to it over the API is just "send it some text,
get some text back". To let it actually DO things, we give it a menu of
named actions it is allowed to request, called "tools" (e.g. "read this
file", "write this JSON", "render this figure"). One round trip works like
this:
  1. We send Claude the conversation so far, plus the list of tools it may
     use.
  2. Claude replies with either plain text, or a request to run one or more
     tools (e.g. "please run tool_read_source with path=X").
  3. WE (this Python code -- never Claude itself) actually run the
     requested tool and see what happens.
  4. We send the result back to Claude as the next message, and repeat.
This repeats until Claude stops asking for tools -- that is its way of
signalling "I believe this step is finished". Each round trip is called a
"turn". An "agent" in this codebase is just: one Claude model + one fixed
list of tools it's allowed to call + a system prompt telling it its job,
looping through turns like this until done.

WHAT "GRAPH" / "NODE" / "EDGE" MEAN HERE
------------------------------------------
Rather than write one big tangled function that decides "what happens
next", the flow between agents is described as a flowchart, using a
library called LangGraph:
  - a NODE is one box in the flowchart -- e.g. "run the Author agent".
  - an EDGE is an arrow from one box to the next -- e.g. "after Author,
    always go to Figure".
  - a CONDITIONAL EDGE is an arrow whose destination depends on what just
    happened -- e.g. "after QA, go to the end if it passed, or back to
    Author to retry if it failed".
This means the SEQUENCE of who runs when is controlled by the flowchart
(the graph), not by any one agent improvising its own plan -- which is the
core difference from the old single-call design.

FILES IN THIS FOLDER, AND THE ORDER A NEW READER SHOULD OPEN THEM
--------------------------------------------------------------------
  1. base.py         -- the shared "run one agent for a few turns" engine
                         used by every agent below. Read this FIRST; every
                         other file builds on it.
  2. prompts.py       -- builds each agent's "system prompt" (its job
                         instructions) and unpacks the study-notes "skill"
                         (a ZIP file of rules/scripts) it's based on.
  3. tools.py         -- every concrete tool an agent is allowed to call
                         (read a file, write JSON, build a figure, compile
                         the .docx, run the quality checks, ...).
  4. stage1_graph.py  -- Stage 1 (Triage/Extraction): decides which chapter
                         a newly-arrived file belongs to.
  5. stage2_graph.py  -- Stage 2 (Generation): the Author/Figure/
                         Compiler/QA flowchart that actually writes one
                         chapter's study notes.
  6. dispatch.py      -- the on/off switch: for each stage, should we run
                         the OLD single-call code, or this NEW graph code?

ROLLOUT SWITCH (how to turn this new code on)
------------------------------------------------
Nothing in this folder runs by default. Two environment variables (read in
config/settings.py) decide, independently, whether Stage 1 and Stage 2 use
this new graph-based code or the old, untouched, single-call code:
    STUDY_NOTES_STAGE1_IMPL=graph   (default: "legacy")
    STUDY_NOTES_STAGE2_IMPL=graph   (default: "legacy")
See dispatch.py for exactly how that choice is made.

DEV_TOKEN_SAVER_MODE (a cheap "does the flowchart work at all?" test)
------------------------------------------------------------------------
Running the real thing costs real API money and takes real time. Setting
the environment variable DEV_TOKEN_SAVER_MODE=1 makes every agent use a
one-sentence fake instruction, a cheap model, a tiny output limit, and
placeholder input text -- so a full run through the whole flowchart,
including the retry loop, costs only a few cents and finishes in seconds.
It is EXPECTED to end in an honest failure (dummy content can't pass the
real quality checks) -- that's the point: it proves the flowchart's wiring
works, it does not attempt to produce real study notes. See the
"DEV_TOKEN_SAVER_MODE" section of docs/migration-to-agents.md for the full
write-up, and grep this folder for DEV_TOKEN_SAVER_MODE to see every place
it changes behaviour.
"""

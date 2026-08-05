"""
src/common/ -- code genuinely shared BY VALUE between two or more of the
three Stage 2 implementations (src/direct_api/, src/agents/,
src/claude_cli_subprocess/), instead of being copy-pasted into each one.

Only put something here if it would otherwise need to be identical (or
near-identical) in more than one implementation. Most of Stage 2 correctly
ISN'T shared -- e.g. how each implementation drives the model (a hand-rolled
tool loop vs LangGraph vs a `claude` CLI subprocess) is genuinely different
per implementation and has no business living here. What's here today
(skill_retro.py) is the autonomous skill-retrospective/self-improvement
machinery: reading tools/retro.py's findings and, opt-in, applying them via
a `claude` CLI session -- the SAME mechanism regardless of which
implementation generated the chapter that produced the findings.
"""

"""
src/claude_cli_subprocess/ -- the THIRD implementation: invoking the `claude`
CLI as a subprocess (billed via Claude subscription, not the metered
Anthropic API key) instead of calling the Anthropic SDK directly
(src/direct_api/) or running a LangGraph flowchart against the SDK
(src/agents/).

Stage 1 (stage1.py): built -- ports the routing logic originally prototyped
in src/assemble_chapters_subtask.py.
Stage 2 (stage2.py): scaffolded only -- raises NotImplementedError; see
docs/cli-subprocess-plan.md for the real design.

common.py holds the one thing both stages need: build_claude_env(), which
strips ANTHROPIC_API_KEY from the subprocess environment so a `claude` CLI
call can never accidentally bill the metered API key instead of the
subscription.

DEV_TOKEN_SAVER_MODE (same shared toggle as src/agents/ and
src/direct_api/): stage1.py uses a dummy prompt (never reads a real file),
skips escalation entirely, and passes --max-budget-usd/--effort low to the
`claude` CLI -- there's no SDK max_tokens parameter to cap, so these two
native CLI flags are the cost-safety equivalent.
"""

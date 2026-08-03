"""
src/direct_api/ -- the "legacy" implementation: direct Anthropic Python SDK
calls (anthropic.Anthropic(), client.messages.create), single-call /
monolithic-loop style. Selected via --stage1-impl legacy / --stage2-impl
legacy (the CLI enum value stays "legacy" for backward compatibility even
though this folder is named direct_api -- see src/agents/dispatch.py).

stage1_api.py: Stage 1 (file routing). stage2_api.py:
Stage 2 (note generation). Both also expose the shared DEV_TOKEN_SAVER_MODE
cost-safe smoke-test toggle (see src/agents/__init__.py for the pattern
this mirrors): cheap model, dummy prompt, capped max_tokens.
"""

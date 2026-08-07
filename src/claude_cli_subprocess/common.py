"""
common.py -- tiny helpers shared by both Stage 1 (stage1_cli.py) and Stage 2
(stage2_cli.py) of this folder's implementation, which drives the `claude`
command-line program as a separate process rather than calling Anthropic's
paid API directly (see src/claude_cli_subprocess/__init__.py for why this
whole folder exists as a third, alternative implementation).
"""

import os
import re
from pathlib import Path


def build_claude_env(extra_env: dict = None, workspace: Path = None) -> dict:
    """Build the set of environment variables to hand to the `claude`
    command-line program when this code launches it as a subprocess (a
    separate, independent running copy of that program).

    WHY: this project can talk to Anthropic's AI in two different ways that
    are billed completely differently -- either metered, pay-per-token API
    calls using an API key (see src/direct_api/ and src/agents/), or
    unmetered calls through a person's own Claude subscription via the
    `claude` command-line tool. If the API key environment variable
    (ANTHROPIC_API_KEY) happened to still be set when `claude` is launched,
    the CLI could pick it up and silently bill the metered API key instead
    of using the subscription -- the opposite of what this implementation
    is for. This function starts from a COPY of the current environment
    variables (so the subprocess still has everything else it needs, like
    PATH) and then deletes ANTHROPIC_API_KEY from that copy, guaranteeing
    the subprocess can never see it.

    extra_env, when given, is merged in last (so callers can set/override
    CLI-behavior env vars such as CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION
    -- see stage2_cli.py's web-enrichment wiring -- without every caller
    needing its own copy-and-pop dance).

    ALSO SETS TWO VARS THE SKILL OTHERWISE HAS TO `export` ITSELF, WHICH IT
    CANNOT (postmortem: docs/stage2-token-burn-postmortem.md). The skill's
    SKILL.md step 1 mandates `export NODE_PATH="$PWD/node_modules"` so
    lib/build.js can resolve the `docx` package, but config.CLAUDE_ALLOWED_TOOLS
    does not (and should not need to) grant bare `export` -- on a real live run
    the model tried it, was denied, and then failed with "Cannot find module
    'docx'". Setting it here removes the need for the model to shell out at
    all. Same story for PYTHONIOENCODING: without it, the skill's Python
    tools crash with UnicodeDecodeError/UnicodeEncodeError on native Windows
    (cp1252 default), and the model's attempt to `export` it was likewise
    denied. Both are set BEFORE extra_env is merged, so an explicit caller
    override still wins.

    workspace, when given, is the per-chapter scratch cwd the `claude`
    subprocess will run in (see stage2_cli._chapter_workspace) -- NODE_PATH is
    derived from it because node_modules is installed per-workspace, not
    globally. Stage 1's router passes no workspace (it never builds a .docx),
    so NODE_PATH is simply not set for it.
    """
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    # setdefault, not assignment: a deliberate ambient override is respected,
    # while the actual bug (unset -> cp1252 on Windows) is fixed.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if workspace is not None:
        env["NODE_PATH"] = str(Path(workspace) / "node_modules")
    if extra_env:
        env.update(extra_env)
    return env


def is_usage_limit(text: str) -> bool:
    """Check whether `text` (the combined output of a `claude` subprocess
    call) looks like it's reporting that the person's Claude subscription
    has hit its usage cap for the current billing window, rather than some
    other kind of failure (a crash, a bad argument, a network error, etc).

    This matters because a usage-limit failure and every other kind of
    failure need to be handled differently by the caller: a usage limit is
    expected to clear up on its own after a few hours, so the caller can
    fall back to a cheaper/offline method and try the real call again
    later, whereas other failures usually mean something is actually broken
    and retrying blindly won't help. Detection is done two ways: (1) a
    specific pattern Claude's usage-limit messages are known to contain
    ("reached | <a long numeric timestamp>"), and (2) a plain-text search
    for a handful of phrases ("usage limit", "rate limit", "5-hour", etc.)
    that also show up in these messages. This is necessarily a best-effort
    guess based on wording, not a structured error code.
    """
    low = (text or "").lower()
    if re.search(r"reached\s*\|\s*\d{9,13}", text or ""):
        return True
    return any(s in low for s in
               ("usage limit", "rate limit", "5-hour", "limit reached",
                "too many requests", "quota exceeded", "session limit"))

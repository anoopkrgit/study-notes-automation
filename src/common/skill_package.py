"""
skill_package.py -- the concurrency-safe "refresh this on disk only if it's
stale" skeleton, shared by the two places that unpack the study-notes skill
zip: src/agents/prompts.ensure_skill_extracted() (extracts to the
.skill-runtime scratch dir the agent tools run scripts from) and
src/claude_cli_subprocess/stage2_cli.sync_skill_package() (installs to the
project-local + global Claude Code skill dirs).

ONLY the locking skeleton lives here. The two callers deliberately keep
their own freshness test and extraction body, which genuinely differ: one
compares an .extracted marker's mtime against the zip's and extracts over
the top; the other compares a .synced_from text stamp and rmtree's first.
What they must NOT each re-implement (and had, before this was factored
out) is the flock + double-checked-lock dance itself -- a subtle bug there
would silently corrupt a half-extracted skill dir under concurrent runs,
and would have to be fixed in two places.
"""

import fcntl
from pathlib import Path
from typing import Callable


def locked_refresh(lock_file: Path, is_fresh: Callable[[], bool], refresh: Callable[[], None]) -> None:
    """Under an exclusive flock on `lock_file`, re-check `is_fresh()` and
    call `refresh()` only if still stale.

    Callers should run their own cheap, lock-free `is_fresh()` check BEFORE
    calling this (to avoid taking the lock at all on the common already-fresh
    path); this repeats the check under the lock to close the race where
    another process refreshed in between. `refresh()` owns the actual
    extraction and writing of whatever freshness marker `is_fresh()` reads.
    """
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    # Mode "a": create the lock file if absent, never truncate an existing
    # one. flock() works regardless of the open mode; we only need a stable
    # fd to lock on.
    with open(lock_file, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            if is_fresh():
                return
            refresh()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

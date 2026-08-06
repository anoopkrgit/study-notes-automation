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

import platform
from pathlib import Path
from typing import Callable

# Platform-branched file locking. `fcntl` does not exist on native Windows
# (importing it at module level made this whole package unimportable there,
# which in turn blocked every entry point that touches the skill zip); Windows
# ships `msvcrt` instead. We import exactly one at module load and expose the
# same lock/unlock pair below so `locked_refresh()` stays platform-agnostic.
_IS_WINDOWS = platform.system() == "Windows"
if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl


def _lock_exclusive(fh) -> None:
    """Take a blocking exclusive lock on the open file handle `fh`."""
    if _IS_WINDOWS:
        # msvcrt.locking locks `nbytes` from the CURRENT file position, so
        # pin it to offset 0 and lock a single byte (the byte need not exist
        # yet -- Windows permits locking a range past EOF). LK_LOCK blocks,
        # retrying for ~10s before raising, which is ample for the brief
        # extraction critical section this guards.
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(fh, fcntl.LOCK_EX)


def _unlock(fh) -> None:
    """Release the lock taken by `_lock_exclusive` on `fh`."""
    if _IS_WINDOWS:
        # Must unlock the exact same byte range that was locked (offset 0,
        # length 1); reset the position first in case `refresh()` moved it.
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh, fcntl.LOCK_UN)


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
    # one. Both flock() and msvcrt.locking() work regardless of the open
    # mode; we only need a stable fd to lock on.
    with open(lock_file, "a") as lf:
        _lock_exclusive(lf)
        try:
            if is_fresh():
                return
            refresh()
        finally:
            _unlock(lf)

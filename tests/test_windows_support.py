"""
Tests for the native-Windows support paths added in
docs/windows-native-support-plan.md:

  - src/common/skill_package.locked_refresh (fcntl/msvcrt lock branch)
  - func_tools_and_utils._run_windows_builtin (ls/cat/cp/mv/mkdir emulation)

The emulation helper is pure Python and OS-independent, so these tests
exercise it directly on any platform (the real dispatch is gated on
platform.system() == "Windows", which is covered by test_tool_bash_dispatch).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

from unittest.mock import patch


# --- locked_refresh -------------------------------------------------------

def test_locked_refresh_runs_when_stale(tmp_path):
    from src.common import skill_package
    calls = []
    skill_package.locked_refresh(
        tmp_path / "x.lock", lambda: False, lambda: calls.append("ran")
    )
    assert calls == ["ran"]


def test_locked_refresh_skips_when_fresh(tmp_path):
    from src.common import skill_package
    calls = []
    skill_package.locked_refresh(
        tmp_path / "x.lock", lambda: True, lambda: calls.append("ran")
    )
    assert calls == []


# --- _run_windows_builtin -------------------------------------------------

def _F():
    from src import func_tools_and_utils as F
    return F


def test_builtin_mkdir_p(tmp_path):
    F = _F()
    out = F._run_windows_builtin(["mkdir", "-p", "a/b/c"], str(tmp_path))
    assert "cleanly" in out
    assert (tmp_path / "a" / "b" / "c").is_dir()


def test_builtin_cat(tmp_path):
    F = _F()
    (tmp_path / "f.txt").write_text("hello\nworld\n")
    assert F._run_windows_builtin(["cat", "f.txt"], str(tmp_path)) == "hello\nworld\n"


def test_builtin_cat_missing(tmp_path):
    F = _F()
    out = F._run_windows_builtin(["cat", "nope.txt"], str(tmp_path))
    assert "No such file" in out


def test_builtin_cp_file(tmp_path):
    F = _F()
    (tmp_path / "a.txt").write_text("x")
    F._run_windows_builtin(["cp", "a.txt", "b.txt"], str(tmp_path))
    assert (tmp_path / "b.txt").read_text() == "x"


def test_builtin_cp_dir_requires_recursive(tmp_path):
    F = _F()
    (tmp_path / "d").mkdir()
    out = F._run_windows_builtin(["cp", "d", "d2"], str(tmp_path))
    assert "-r not specified" in out
    assert not (tmp_path / "d2").exists()


def test_builtin_cp_recursive(tmp_path):
    F = _F()
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "d" / "sub" / "f").write_text("y")
    F._run_windows_builtin(["cp", "-r", "d", "d2"], str(tmp_path))
    assert (tmp_path / "d2" / "sub" / "f").read_text() == "y"


def test_builtin_mv(tmp_path):
    F = _F()
    (tmp_path / "a.txt").write_text("z")
    F._run_windows_builtin(["mv", "a.txt", "b.txt"], str(tmp_path))
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").read_text() == "z"


def test_builtin_ls(tmp_path):
    F = _F()
    (tmp_path / "one.txt").touch()
    (tmp_path / "two.txt").touch()
    out = F._run_windows_builtin(["ls"], str(tmp_path))
    assert out.splitlines() == ["one.txt", "two.txt"]


def test_tool_bash_dispatches_to_builtin_on_windows(tmp_path):
    """On Windows, tool_bash services coreutils via _run_windows_builtin and
    never shells out; elsewhere it uses subprocess as before."""
    F = _F()
    (tmp_path / "a.txt").write_text("hi")
    with patch.object(F, "_IS_WINDOWS", True), \
            patch.object(F, "subprocess") as mock_sub:
        out = F.tool_bash("cat a.txt", cwd=str(tmp_path))
    assert out == "hi"
    mock_sub.run.assert_not_called()
